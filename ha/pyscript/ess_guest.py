"""Guest charging: session counter for energy charged into a guest car and the cost of its grid import.

While input_boolean.ess_ev_guest_mode is on, the executor feeds the charger from the grid (the house battery
never discharges into it). PV surplus that the battery cannot absorb flows into the car by itself.
Every 10 s this script integrates:
  charged energy  = EVCS power
  grid energy     = min(EVCS power, grid import measured by the P1 meter); the rest counts as solar
  cost            = grid energy x sensor.ess_import_price (spot + fees + VAT; fixed monthly fees excluded)
The session starts when guest mode is switched on and ends when it is switched off or the guest car is
unplugged (guest mode then switches itself off and a summary is sent). Stored in pyscript.ess_guest_charging.
"""

import time

import homeassistant.util.dt as dt_util

GUEST = "input_boolean.ess_ev_guest_mode"
SESSION = "pyscript.ess_guest_charging"
EV_POWER = "sensor.victronevcharger_total_power40"
EV_STATUS = "sensor.victronevcharger_status40"
GRID = "sensor.grid_power"  # P1 meter, W, + = import
PRICE = "sensor.ess_import_price"
NOTIFY = "mobile_app_your_phone"
MAX_GAP_S = 30  # longer gaps (restarts) are not integrated

EMPTY = {"active": False, "started": None, "ended": None, "energy_kwh": 0.0, "grid_kwh": 0.0, "solar_kwh": 0.0,
         "cost_sek": 0.0, "avg_price": None, "power_w": 0, "last_session": None,
         "unit_of_measurement": "SEK", "friendly_name": "ESS guest charging", "icon": "mdi:account-cash"}

state.persist(SESSION, default_value="0", default_attributes=EMPTY)
G = {"last_tick": None, "last_status": None, "connected": False}


def _num(entity, default=0.0):
    try:
        return float(state.get(entity))
    except (TypeError, ValueError, NameError):
        return default


def _attrs():
    attrs = dict(EMPTY)
    attrs.update(state.getattr(SESSION) or {})
    return attrs


def _publish(attrs):
    attrs["avg_price"] = round(attrs["cost_sek"] / attrs["grid_kwh"], 3) if attrs["grid_kwh"] > 0.01 else None
    state.set(SESSION, round(attrs["cost_sek"], 2), attrs)


def _summary(attrs):
    return (f"{attrs['energy_kwh']:.2f} kWh charged ({attrs['grid_kwh']:.2f} kWh grid, "
            f"{attrs['solar_kwh']:.2f} kWh solar), grid cost {attrs['cost_sek']:.2f} SEK")


@state_trigger(GUEST)
def ess_guest_toggled(value=None, old_value=None):
    attrs = _attrs()
    now = dt_util.now().isoformat()
    if value == "on" and not attrs["active"]:
        last = attrs.get("last_session")
        attrs = dict(EMPTY)
        attrs.update({"active": True, "started": now, "last_session": last})
        G["last_tick"], G["connected"] = time.monotonic(), False
        _publish(attrs)
        log.info("ess_guest: guest session started")
    elif value == "off" and attrs["active"]:
        attrs["active"], attrs["ended"], attrs["power_w"] = False, now, 0
        if attrs["energy_kwh"] > 0.05:  # an empty session must not overwrite the last real one
            attrs["last_session"] = {k: (round(attrs[k], 3) if isinstance(attrs[k], float) else attrs[k])
                                     for k in ("started", "ended", "energy_kwh", "grid_kwh", "solar_kwh", "cost_sek")}
        else:
            attrs["started"] = attrs["ended"] = None
        _publish(attrs)
        if attrs["energy_kwh"] > 0.05:
            service.call("notify", NOTIFY, title="🔌 Guest charging finished", message=_summary(attrs))
        log.info(f"ess_guest: guest session ended: {_summary(attrs)}")


@time_trigger("period(now, 10s)")
def ess_guest_tick():
    if state.get(GUEST) != "on":
        G["last_tick"] = None
        return
    attrs = _attrs()
    if not attrs["active"]:  # guest mode was on before this script loaded (e.g. restart): resume the session
        attrs["active"] = True
        attrs["started"] = attrs["started"] or dt_util.now().isoformat()
    now = time.monotonic()
    dt_s = now - G["last_tick"] if G["last_tick"] is not None else 0.0
    G["last_tick"] = now
    ev_w = max(_num(EV_POWER), 0.0)
    if 0 < dt_s <= MAX_GAP_S and ev_w > 0:
        grid_w = min(ev_w, max(_num(GRID), 0.0))
        h = dt_s / 3600
        attrs["energy_kwh"] += ev_w * h / 1000
        attrs["grid_kwh"] += grid_w * h / 1000
        attrs["solar_kwh"] += (ev_w - grid_w) * h / 1000
        attrs["cost_sek"] += grid_w * h / 1000 * _num(PRICE)
    attrs["power_w"] = round(ev_w)
    _publish(attrs)


@state_trigger(EV_STATUS)
def ess_guest_unplugged(value=None):
    """Guest car unplugged after having been connected: end guest mode (ignores Modbus dropouts)."""
    if value in ("unavailable", "unknown", None):
        return
    if state.get(GUEST) == "on":
        if value != "DISCONNECTED":
            G["connected"] = True
        elif G["connected"] and G["last_status"] not in (None, "DISCONNECTED"):
            G["connected"] = False
            input_boolean.turn_off(entity_id=GUEST)
    G["last_status"] = value


@time_trigger("startup")
def ess_guest_startup():
    status = state.get(EV_STATUS)
    if status not in ("unavailable", "unknown", None):
        G["last_status"] = status
        G["connected"] = status != "DISCONNECTED"
