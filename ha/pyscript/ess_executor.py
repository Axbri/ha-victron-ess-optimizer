"""ESS executor: turns the requested mode into Victron ESS and EVCS register writes.

docs/executor.md. The requested mode is input_select.ess_mode; 'Auto' follows the optimizer's plan
(sensor.ess_plan) for the current quarter and falls back to self-consumption when the plan is missing or stale. A register is written only when its value must change (deadband) and at
most once per 5 s per entity. Status is published in sensor.ess_executor and sensor.ess_ev.
"""

import datetime as dt
import math
import uuid

import homeassistant.util.dt as dt_util

# Victron registers (via the Victron GX Modbus integration)
SP = "number.victron_settings_ess_acpowersetpoint"
MINSOC = "number.victron_settings_ess_batterylife_minimumsoc"
DVCC = "number.victron_settings_ess_maxchargecurrent"
PREVENT_FEEDBACK = "switch.victron_settings_ess_preventfeedback"  # blocks AC-coupled PV feed-in only
DC_FEEDIN = "switch.victron_settings_ess_overvoltagefeedin"  # feed-in of DC (MPPT) PV surplus
SOC = "sensor.victron_battery_soc_223"
EVCS_SWITCH = "switch.victron_evcharger_startstop_40"
EVCS_CURRENT = "number.victron_evcharger_setcurrent_40"
EVCS_MAX_CURRENT = "number.victron_evcharger_maxcurrent_40"
EVCS_POWER = "sensor.victronevcharger_total_power40"
EVCS_STATUS = "sensor.victronevcharger_status40"
CAR_SOC = "sensor.ev_soc"
CAR_WAKE = "button.ev_charge_start"
CAR_PLUGGED = "binary_sensor.ev_plugged_in"
PREHEAT = "input_boolean.ess_ev_preheat"  # warm the car before departures (workday or manual), fed from the charger
CAR_CLIMATE_EXT = "binary_sensor.ev_climate_active"
CAR_CLIMATE_EXT_START = "button.ev_climate_start"
CAR_CLIMATE_STOP = "button.ev_climate_stop"
CAR_OUTDOOR_TEMP = "sensor.ev_outdoor_temperature"  # the car's own sensor, preferred for the preheat lead time
EXPORT_PRICE = "sensor.ess_export_price"
WORKDAY = "binary_sensor.workday_sensor"
WEATHER = "weather.forecast_home"
OUTDOOR_TEMP = "sensor.outdoor_temperature"
NOTIFY = "mobile_app_your_phone"
GUEST = "input_boolean.ess_ev_guest_mode"  # charge another car: start at once, power from the grid, no EV logic
MANUAL_DEP = "input_boolean.ess_ev_manual_departure"  # one-off departure with its own target (e.g. a weekend trip)
MANUAL_DEP_TIME = "input_datetime.ess_ev_manual_departure_time"
MANUAL_TARGET = "input_number.ess_ev_manual_target"

# The car's top end, measured 2026-11-03..12 (8 nights): it stops between 93 and 97.5 % (soc_raw), and above ~92 %
# each percent takes ~8 min longer than at full power (charge power falls from 3.4 kW to below 1 kW).
EV_MAX_SOC = "input_number.ess_ev_max_reachable_soc"  # highest target the car reaches; higher targets are capped to it
EV_TAPER_FROM = 92.0  # % SoC
EV_TAPER_MIN_PER_PCT = 8.0  # extra minutes per percent above EV_TAPER_FROM
EV_REACHED_SLACK = 5.0  # % below the target that still counts as reached once it was reached (preheat uses 1-4 %)

MANUAL_MODES = ("Self-consumption", "Hold", "Charge now", "Export now")
VOLATILE_MODES = ("Charge now", "Export now")  # reset to Auto when the executor starts
EVCS_AWAY = ("DISCONNECTED", "unavailable", "unknown", None)

INSTANCE = uuid.uuid4().hex[:8]
STARTED = dt_util.now()

S = {
    "last": {},  # entity -> (value, written_at)
    "writes": [],  # timestamps of register writes
    "effective": None,
    "released_at": None,
    "hold_level": None,
    "export_stopped": False,
    "notified": {},
    "cache": {},
    "ev": {"at_home": False, "soc": None, "want_since": None, "wakes": 0, "last_wake": None, "override_for": None},
}


def _get(entity):
    try:
        return state.get(entity)
    except NameError:
        return None


def _num(entity, default=None):
    try:
        return float(_get(entity))
    except (TypeError, ValueError):
        return default


def _on(entity):
    return _get(entity) == "on"


def _local_dt(text):
    """Parse an input_datetime / ISO string as local time."""
    try:
        parsed = dt_util.parse_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
    return parsed


def _notify(key, title, message, every_hours=6):
    now = dt_util.now()
    last = S["notified"].get(key)
    if last and (now - last).total_seconds() < every_hours * 3600:
        return
    S["notified"][key] = now
    service.call("notify", NOTIFY, title=title, message=message)


def _write_number(entity, value, deadband=0.5):
    """Write only when needed. Returns True if a write was sent."""
    now = dt_util.now()
    current = _num(entity)
    if current is None:
        return False  # entity unavailable: keep the last command, retry next loop
    last = S["last"].get(entity)
    if last and abs(last[0] - value) <= deadband and (now - last[1]).total_seconds() < 60:
        return False  # written recently; the entity may still show the old value (readback lags one poll)
    if abs(current - value) <= deadband:
        S["last"][entity] = (value, now)
        return False
    if last and (now - last[1]).total_seconds() < 5:
        return False
    service.call("number", "set_value", entity_id=entity, value=value)
    S["last"][entity] = (value, now)
    S["writes"].append(now)
    log.info(f"ess_executor: {entity} := {value} (was {current})")
    return True


def _write_switch(entity, turn_on):
    now = dt_util.now()
    current = _get(entity)
    if current not in ("on", "off"):
        return False
    value = 1.0 if turn_on else 0.0
    last = S["last"].get(entity)
    if last and last[0] == value and (now - last[1]).total_seconds() < 60:
        return False
    if (current == "on") == turn_on:
        S["last"][entity] = (value, now)
        return False
    if last and (now - last[1]).total_seconds() < 5:
        return False
    service.call("switch", "turn_on" if turn_on else "turn_off", entity_id=entity)
    S["last"][entity] = (value, now)
    S["writes"].append(now)
    log.info(f"ess_executor: {entity} := {'on' if turn_on else 'off'}")
    return True


def _is_workday(day):
    key = ("workday", day.isoformat())
    if key not in S["cache"]:
        resp = hass.services.async_call(
            "workday", "check_date", {"entity_id": WORKDAY, "check_date": day.isoformat()},
            blocking=True, return_response=True,
        )
        S["cache"][key] = bool(resp.get(WORKDAY, {}).get("workday"))
    return S["cache"][key]


def _temperature_at(when):
    """Forecast temperature for the hour closest to `when`; current outdoor temperature as fallback."""
    now = dt_util.now()
    cached = S["cache"].get("forecast")
    if not cached or (now - cached[0]).total_seconds() > 1800:
        try:
            resp = hass.services.async_call(
                "weather", "get_forecasts", {"entity_id": WEATHER, "type": "hourly"},
                blocking=True, return_response=True,
            )
            cached = (now, resp[WEATHER]["forecast"])
        except Exception as err:  # weather service unavailable
            log.warning(f"ess_executor: weather forecast failed: {err}")
            cached = (now, [])
        S["cache"]["forecast"] = cached
    best = None
    for item in cached[1]:
        start = _local_dt(item.get("datetime"))
        if start is None:
            continue
        diff = abs((start - when).total_seconds())
        if diff <= 5400 and (best is None or diff < best[0]):
            best = (diff, item.get("temperature"))
    if best and best[1] is not None:
        return float(best[1]), "forecast"
    return _num(OUTDOOR_TEMP), "outdoor sensor"


def _next_departure(now):
    """Next departure the EV must be ready for, honouring the one-shot override."""
    dep_time = dt.time.fromisoformat(_get("input_datetime.ess_ev_departure") or "06:30:00")
    override = _get("input_select.ess_ev_next_departure") or "Auto"
    first = None
    for days in range(0, 9):
        day = now.date() + dt.timedelta(days=days)
        candidate = dt.datetime.combine(day, dep_time, tzinfo=dt_util.get_default_time_zone())
        if candidate <= now:
            continue
        if first is None:
            first = candidate
            if override != "Auto":
                # The override applies to the first calendar departure only, then resets to Auto.
                if S["ev"]["override_for"] is None:
                    S["ev"]["override_for"] = candidate
                if override == "Yes":
                    return candidate, True
                continue
        if _is_workday(day):
            return candidate, _is_workday(day)
    return None, False


def _manual_departure(now):
    """The owner's one-off departure (2026-11-14): (departure, target %, note) while MANUAL_DEP is on and its time is
    ahead. Switched off once a time it was used for has passed, so it applies to one trip only; switched on with a time
    already in the past, it stays on and waits for a new time (it used to switch straight off again)."""
    ev = S["ev"]
    if not _on(MANUAL_DEP):
        ev["manual_for"] = None
        return None, None
    dep = _local_dt(_get(MANUAL_DEP_TIME))
    if dep is None or dep <= now:
        if dep is not None and ev.get("manual_for") == dep:
            ev["manual_for"] = None
            input_boolean.turn_off(entity_id=MANUAL_DEP)
            return None, None
        return None, "time passed"
    ev["manual_for"] = dep
    return (dep, int(_num(MANUAL_TARGET, 80))), "active"


def _ev_control(now, ev_power, passive=False):
    """EV control. passive (house mode 'Victron default', or ess_ev_smart_control off): keep the charger
    enabled and let the car's own timer decide, as before this system; only guest mode still starts it."""
    ev = S["ev"]
    mode = _get("input_select.ess_ev_mode") or "Auto"

    # Reset the one-shot departure override once its departure has passed.
    if ev["override_for"] is not None and now >= ev["override_for"]:
        ev["override_for"] = None
        if _get("input_select.ess_ev_next_departure") != "Auto":
            input_select.select_option(entity_id="input_select.ess_ev_next_departure", option="Auto")

    status_raw = _get(EVCS_STATUS)
    if status_raw in ("unavailable", "unknown", None):
        at_home = ev["at_home"]  # EVCS Modbus drops out briefly; keep the last known state
        ev["offline_since"] = ev.get("offline_since") or now
        if (now - ev["offline_since"]).total_seconds() > 900 and _on(CAR_PLUGGED):
            _notify("evcs_offline", "EV charger offline",
                    f"The EVCS has not answered via the GX since {ev['offline_since'].strftime('%H:%M')}, so HA "
                    "cannot control it. Check its Wi-Fi and that its GX connection is set to Modbus TCP.")
    else:
        ev["offline_since"] = None
        at_home = status_raw not in EVCS_AWAY
    was_home = ev["at_home"]
    ev["at_home"] = at_home
    charging = status_raw == "CHARGING" or ev_power > 500

    if _on(GUEST):
        # Guest car: enable the charger at the guest current; no EV SoC, deadline guard or CAN wake.
        ev["want_since"], ev["wakes"] = None, 0
        if at_home:
            _write_switch(EVCS_SWITCH, True)
            _write_number(EVCS_CURRENT, min(_num("input_number.ess_ev_guest_current", 16), _num(EVCS_MAX_CURRENT, 16)))
        status = "Guest: charging" if charging else ("Guest: connected, not charging" if at_home else "Guest: waiting for car")
        state.set("sensor.ess_ev", status, {
            "friendly_name": "ESS EV status", "icon": "mdi:account-arrow-right", "mode": "Guest",
            "at_home": at_home, "charging": charging, "power_w": round(ev_power), "guard_active": False,
        })
        return

    if passive:
        ev["want_since"], ev["wakes"] = None, 0
        if at_home:
            _write_switch(EVCS_SWITCH, True)
            _write_number(EVCS_CURRENT, _num(EVCS_MAX_CURRENT, 16))
        status = ("Charging" if charging else "Plugged in") if at_home else "Away"
        state.set("sensor.ess_ev", status, {
            "friendly_name": "ESS EV status", "icon": "mdi:car-electric", "mode": "Passive (Victron default)",
            "at_home": at_home, "charging": charging, "power_w": round(ev_power), "guard_active": False,
        })
        return

    soc = _num(CAR_SOC)
    soc_note = "car SoC"
    if soc is None:
        soc = ev["soc"]
        soc_note = "last known"
    if soc is None:
        soc = 0.0
        soc_note = "unknown (assumed 0 %)"
    ev["soc"] = soc

    departure, workday = _next_departure(now)
    # A manual departure is used when it comes first; the workday departure after it is planned once it has passed.
    workday_departure = departure  # the calendar departure, also published for the wake-up (ignores manual ones)
    manual, manual_note = _manual_departure(now)
    dep_source = "workday"
    if manual is not None:
        if departure is None or manual[0] <= departure:
            departure, workday, dep_source = manual[0], False, "manual"
        else:
            manual_note = "after workday"
    # Target: the manual target for a manual departure; otherwise linear in the temperature at departure between
    # (cold temp -> cold target) and (warm temp -> warm target), clamped outside that range. Summer/Winter force the warm/cold target.
    season = _get("input_select.ess_ev_season") or "Auto"
    target_cold = _num("input_number.ess_ev_target_winter", 100)
    target_warm = _num("input_number.ess_ev_target_summer", 70)
    temp, temp_source = (None, None)
    if dep_source == "manual":
        target = manual[1]
    elif season == "Auto":
        temp, temp_source = _temperature_at(departure or now)
        t_cold, t_warm = _num("input_number.ess_ev_cold_temp", 0), _num("input_number.ess_ev_warm_temp", 20)
        if temp is None or t_warm <= t_cold:
            frac = 0.0  # no temperature: be safe and use the cold target
        else:
            frac = min(1.0, max(0.0, (temp - t_cold) / (t_warm - t_cold)))
        target = math.ceil(target_cold + (target_warm - target_cold) * frac)
    else:
        target = target_cold if season == "Winter" else target_warm
    target_wanted = target
    target = min(target, int(_num(EV_MAX_SOC, 95)))  # the car stops below 100 %: aim for what it can reach
    kwh_per_pct, power_w = _num("input_number.ess_ev_kwh_per_percent", 0.27), _num("input_number.ess_ev_charge_power", 3600)
    learned = state.getattr("sensor.ess_ev_learning") if _get("sensor.ess_ev_learning") not in (None, "unknown") else None
    if _on("input_boolean.ess_ev_use_learned") and learned and learned.get("sessions", 0) >= 3:
        kwh_per_pct = _num("sensor.ess_ev_learning", kwh_per_pct)
        power_w = learned.get("power_w") or power_w
    power_kw = max(power_w / 1000, 0.5)
    needed_kwh = max(0.0, target - soc) * kwh_per_pct
    # Target reached for this departure: it stays reached while the SoC is within EV_REACHED_SLACK of it. The preheat
    # lowers the SoC by 1-4 % and the car may stop on its own just below the target (EVCS status CHARGED); neither
    # should restart the charge or the deadline guard. Cleared when the car is unplugged or plugged in.
    # Also cleared when the target rises (a colder forecast).
    if not at_home or not was_home:
        ev["reached_for"] = None
    elif departure is not None and (needed_kwh <= 0 or (status_raw == "CHARGED" and soc >= target - EV_REACHED_SLACK)):
        if ev.get("reached_for") != departure:
            ev["reached_for"], ev["reached_target"] = departure, target
    reached = (departure is not None and ev.get("reached_for") == departure
               and target <= ev.get("reached_target", 0) + 1 and soc >= target - EV_REACHED_SLACK)
    if reached:
        needed_kwh = 0.0
    # The slow top end is planned as extra time at full power, so the optimizer reserves the quarters for it.
    taper_min = EV_TAPER_MIN_PER_PCT * max(0.0, target - max(soc, EV_TAPER_FROM)) if needed_kwh > 0 else 0.0
    needed_kwh += taper_min / 60 * power_kw
    duration_h = needed_kwh / power_kw
    margin = dt.timedelta(minutes=_num("input_number.ess_ev_finish_margin", 30))
    latest_start = None
    guard = False
    if departure is not None and needed_kwh > 0:
        latest_start = departure - margin - dt.timedelta(hours=duration_h)
        guard = at_home and now >= latest_start
        if guard and now > latest_start + dt.timedelta(minutes=10) and not charging:
            _notify("ev_late", "EV: target at risk",
                    f"EV {soc:.0f} % needs {needed_kwh:.1f} kWh to reach {target:.0f} % by "
                    f"{(departure - margin).strftime('%H:%M')}, but it is not charging.", every_hours=3)

    preheat, preheat_note = _preheat(now, at_home, departure, mode)

    want_charge = False
    next_planned = None
    if mode == "Off":
        status = "Off"
        if at_home:
            _write_switch(EVCS_SWITCH, False)
            if guard:
                _notify("ev_off", "EV charging is off",
                        f"EV mode is Off; the car ({soc:.0f} %) will not reach {target:.0f} % by departure.")
    elif mode == "Charge now":
        if was_home and not at_home:
            input_select.select_option(entity_id="input_select.ess_ev_mode", option="Auto")
        want_charge = at_home and soc < 100
        status = "Charge now" if at_home else "Charge now (not plugged in)"
    else:  # Auto: the EVCS is on only in the quarters the optimizer planned for the EV, or for the deadline guard
        planned, next_planned, plan_note = _ev_plan_now(now)
        want_charge = at_home and (guard or planned)
        if at_home and not want_charge and not preheat:
            _write_switch(EVCS_SWITCH, False)
        if not at_home:
            status = "Away"
        elif planned:
            status = "Charging (planned)"
        elif guard:
            status = "Deadline guard: charging"
        elif plan_note:
            status = f"Waiting ({plan_note}; deadline guard only)"
        elif next_planned:
            status = f"Waiting for planned charge {next_planned.strftime('%a %H:%M')}"
        elif needed_kwh <= 0:
            status = "Target reached"
        else:
            status = "Waiting (no charge planned yet)"

    # Start charging and wake the car when it does not respond.
    if want_charge:
        _write_switch(EVCS_SWITCH, True)
        _write_number(EVCS_CURRENT, _num(EVCS_MAX_CURRENT, 16))
        if charging:
            ev["want_since"], ev["wakes"] = None, 0
        else:
            ev["want_since"] = ev["want_since"] or now
            waited = (now - ev["want_since"]).total_seconds()
            since_wake = (now - ev["last_wake"]).total_seconds() if ev["last_wake"] else 1e9
            if waited > 180 and ev["wakes"] < 2 and since_wake > 180:
                button.press(entity_id=CAR_WAKE)
                ev["wakes"] += 1
                ev["last_wake"] = now
                log.info("ess_executor: EV did not start charging; sent CAN charge_start")
            elif ev["wakes"] >= 2 and since_wake > 180:
                _notify("ev_no_start", "EV: EV does not charge",
                        "The charger is enabled but the car did not start charging after two wake attempts.")
    else:
        ev["want_since"], ev["wakes"] = None, 0
        if preheat:  # the charger feeds the climate (and may top up the car), so the car's battery is not used
            _write_switch(EVCS_SWITCH, True)
            _write_number(EVCS_CURRENT, _num(EVCS_MAX_CURRENT, 16))

    state.set("sensor.ess_ev", status, {
        "friendly_name": "ESS EV status",
        "icon": "mdi:car-electric",
        "mode": mode,
        "at_home": at_home,
        "charging": charging,
        "power_w": round(ev_power),
        "soc": round(soc, 1),
        "soc_source": soc_note,
        "next_departure": departure.isoformat() if departure else None,
        "departure_is_workday": workday,
        "departure_source": dep_source,
        "manual_departure": manual_note,  # None (off), active, after workday, time passed
        "workday_departure": workday_departure.isoformat() if workday_departure else None,
        "season": season.lower(),
        "temperature": temp,
        "temperature_source": temp_source,
        "target": target,
        "target_wanted": target_wanted,
        "target_reached": reached,
        "taper_min": round(taper_min),
        "needed_kwh": round(needed_kwh, 2),
        "kwh_per_percent": kwh_per_pct,
        "charge_power_w": power_w,
        "latest_start": latest_start.isoformat() if latest_start else None,
        "guard_active": guard,
        "next_planned_charge": next_planned.isoformat() if next_planned else None,
        "preheat": preheat_note,
    })


def _preheat(now, at_home, departure, mode):
    """EV preheat before a departure (owner, 2026-10-31): extended climate from 10 min before departure at 20 °C to
    30 min at -10 °C, linear and extrapolated below -10; none above 20 °C. Only while plugged in, with the charger on,
    so the heat comes from the house/grid and not the car's battery. Returns (keep the charger on, status note)."""
    ev = S["ev"]
    p = ev.get("preheat")
    if p and now >= p["dep"] + dt.timedelta(minutes=30):
        # Still plugged in 30 min after the departure: the trip did not happen, so stop the climate.
        if p["sent"] and at_home and _on(CAR_CLIMATE_EXT):
            button.press(entity_id=CAR_CLIMATE_STOP)
            log.info("ess_executor: preheat stopped, the car is still plugged in 30 min after departure")
        ev["preheat"] = p = None
    if p and p["sent"] and now >= p["dep"]:
        return at_home, "Preheat: running past departure"  # keep feeding it until the car leaves or 30 min pass
    if not _on(PREHEAT) or mode == "Off" or departure is None:
        return False, None
    # The car's outdoor sensor (owner, 2026-10-31: more accurate than the forecast) while it is fresh, i.e. reported
    # within 3 h (the CAN bus sleeps when the car is idle); otherwise the forecast for the departure.
    temp, src = _num(CAR_OUTDOOR_TEMP), "car"
    st = hass.states.get(CAR_OUTDOOR_TEMP)
    if temp is None or st is None or (now - st.last_reported).total_seconds() > 3 * 3600:
        temp, src = _temperature_at(departure)[0], "forecast"
    if temp is None or temp > 20:
        return False, f"Preheat: not needed ({temp:.0f} °C {src})" if temp is not None else None
    lead = 10 + (20 - temp) * 20 / 30
    start = departure - dt.timedelta(minutes=lead)
    note = f"Preheat {start.strftime('%a %H:%M')} ({lead:.0f} min, {temp:.0f} °C {src})"
    if now < start:
        return False, note
    if not at_home:
        return False, note + ": not plugged in, skipped"
    if not p or p["dep"] != departure:
        p = ev["preheat"] = {"dep": departure, "since": now, "sent": None, "tries": 0}
    # Give the charger ~20 s to switch on before the climate starts, then retry twice if it does not report on.
    if _on(CAR_CLIMATE_EXT):
        return True, f"Preheat running since {start.strftime('%H:%M')} ({lead:.0f} min, {temp:.0f} °C {src})"
    if (now - p["since"]).total_seconds() >= 20 and p["tries"] < 3 and (
            p["sent"] is None or (now - p["sent"]).total_seconds() > 120):
        button.press(entity_id=CAR_CLIMATE_EXT_START)
        p["sent"], p["tries"] = now, p["tries"] + 1
        log.info(f"ess_executor: preheat started ({lead:.0f} min before departure at {temp:.1f} °C, try {p['tries']})")
    elif p["tries"] >= 3 and (now - p["sent"]).total_seconds() > 120:
        _notify("ev_preheat", "EV: preheat failed", "The car did not start its climate after three attempts.")
    return True, note + ": starting"


def _ev_plan_now(now):
    """(charge in this quarter, start of the next planned EV quarter, note) from sensor.ess_plan's ev_kw.
    Without a fresh plan nothing is planned (note says why); the deadline guard still protects the departure."""
    try:
        attrs = state.getattr("sensor.ess_plan") or {}
    except NameError:
        return False, None, "no optimizer plan"
    computed, start = _local_dt(attrs.get("computed_at")), _local_dt(attrs.get("start"))
    if computed is None or start is None:
        return False, None, "no optimizer plan"
    if (now - computed).total_seconds() > 45 * 60:
        return False, None, "plan older than 45 min"
    ev_kw = attrs.get("ev_kw") or []
    idx = int((now - start).total_seconds() // 900)
    if idx < 0 or idx >= len(ev_kw):
        return False, None, "plan does not cover this quarter"
    later = [i for i in range(idx + 1, len(ev_kw)) if ev_kw[i] > 0]
    next_planned = start + dt.timedelta(minutes=15 * later[0]) if later else None
    return ev_kw[idx] > 0, next_planned, None


@time_trigger("startup")
def ess_executor_startup():
    mode = _get("input_select.ess_mode")
    if mode in VOLATILE_MODES:
        log.warning(f"ess_executor: start with mode '{mode}' -> Auto")
        input_select.select_option(entity_id="input_select.ess_mode", option="Auto")
    ess_control("startup")


@state_trigger("input_select.ess_mode")
def ess_mode_changed(value=None, old_value=None):
    S["hold_level"] = None
    S["export_stopped"] = False
    hours = _num("input_number.ess_override_hours", 0)
    if value in MANUAL_MODES and hours > 0:
        until = dt_util.now() + dt.timedelta(hours=hours)
        input_datetime.set_datetime(entity_id="input_datetime.ess_override_until",
                                    datetime=until.strftime("%Y-%m-%d %H:%M:%S"))
    ess_control(f"mode {old_value} -> {value}")


@state_trigger(
    "input_select.ess_ev_mode", "input_select.ess_ev_season", "input_select.ess_ev_next_departure",
    "input_boolean.ess_allow_solar_export", "input_boolean.ess_block_export_negative_price",
    "input_boolean.ess_allow_battery_to_ev", "input_number.ess_min_soc", "input_number.ess_manual_power",
    "input_number.ess_manual_export_floor", "input_number.ess_grid_offset", "input_number.ess_max_soc_grid_charge",
    "sensor.victronevcharger_status40", "input_boolean.ess_ev_guest_mode", "input_number.ess_ev_guest_current",
    "input_boolean.ess_ev_smart_control", "input_boolean.ess_allow_battery_export", "input_number.ess_sell_spike_price",
    "input_boolean.ess_ev_manual_departure", "input_datetime.ess_ev_manual_departure_time",
    "input_number.ess_ev_manual_target",
)
def ess_input_changed(var_name=None):
    ess_control(f"changed: {var_name}")


@time_trigger("period(now, 10s)")
def ess_tick():
    ess_control("tick")


@service
def ess_executor_keepalive():
    """Called by the HA automation "ESS Pyscript keepalive" when the 10 s loop has stalled. Pyscript time triggers
    freeze for the repeated autumn DST hour (they wait for local wall-clock time to catch up, 2026-10-25)."""
    ess_control("keepalive")


PLAN_MODES = {"S": "SELF", "H": "HOLD", "C": "CHARGE", "E": "EXPORT", "X": "SOLAR_EXPORT"}


def _plan_step(now):
    """The optimizer's decision for the current quarter: (dict, None) or (None, reason)."""
    try:
        attrs = state.getattr("sensor.ess_plan") or {}
    except NameError:
        return None, "no optimizer plan"
    computed, start = _local_dt(attrs.get("computed_at")), _local_dt(attrs.get("start"))
    if computed is None or start is None:
        return None, "no optimizer plan"
    if (now - computed).total_seconds() > 45 * 60:
        return None, "plan older than 45 min"
    idx = int((now - start).total_seconds() // 900)
    modes = attrs.get("modes") or ""
    if idx < 0 or idx >= len(modes):
        return None, "plan does not cover this quarter"
    grid = attrs.get("grid_kw") or []
    floor = attrs.get("floor") or []
    return {"mode": PLAN_MODES.get(modes[idx], "SELF"),
            "grid_w": (grid[idx] if idx < len(grid) else 0.0) * 1000,
            "floor": floor[idx] if idx < len(floor) else 100,
            "summary": attrs.get("summary") or []}, None


def _superseded():
    """True when a newer executor instance (e.g. left over from a reload) owns the control."""
    try:
        attrs = state.getattr("sensor.ess_executor") or {}
    except NameError:
        return False
    other, other_started = attrs.get("instance"), _local_dt(attrs.get("started"))
    if other and other != INSTANCE and other_started and other_started > STARTED:
        if not S.get("superseded_logged"):
            S["superseded_logged"] = True
            log.warning(f"ess_executor: instance {INSTANCE} superseded by {other}; standing down")
        return True
    return False


def ess_control(trigger="manual"):
    task.unique("ess_control")
    if _superseded():
        return
    now = dt_util.now()
    S["writes"] = [t for t in S["writes"] if (now - t).total_seconds() < 3600]

    mode = _get("input_select.ess_mode") or "Auto"
    if mode in MANUAL_MODES and _num("input_number.ess_override_hours", 0) > 0:
        until = _local_dt(_get("input_datetime.ess_override_until"))
        if until is not None and now >= until:
            log.info(f"ess_executor: override '{mode}' expired -> Auto")
            input_select.select_option(entity_id="input_select.ess_mode", option="Auto")
            mode = "Auto"

    soc = _num(SOC)
    ev_power = _num(EVCS_POWER, 0.0) or 0.0
    try:
        _ev_control(now, ev_power, passive=mode == "Victron default" or not _on("input_boolean.ess_ev_smart_control"))
    except Exception as err:  # never let EV logic stop the house battery control
        log.error(f"ess_executor: EV control failed: {err}")
        state.set("sensor.ess_ev", "error", {"friendly_name": "ESS EV status", "error": str(err)})

    min_soc = _num("input_number.ess_min_soc", 8)
    offset = _num("input_number.ess_grid_offset", 0)
    export_price = _num(EXPORT_PRICE)
    solar_export = _on("input_boolean.ess_allow_solar_export") and not (
        _on("input_boolean.ess_block_export_negative_price") and export_price is not None and export_price < 0
    )
    ev_from_grid = ev_power > 300 and (_on(GUEST) or not _on("input_boolean.ess_allow_battery_to_ev"))
    ev_offset = ev_power if ev_from_grid else 0.0

    if mode == "Victron default":
        if S["effective"] != "RELEASED":
            S["released_at"] = now
        if (now - S["released_at"]).total_seconds() < 60:  # release (with retries), then leave the registers alone
            _write_number(SP, 0, 1)
            _write_number(MINSOC, min_soc)
            _write_number(DVCC, -1)
            _write_switch(PREVENT_FEEDBACK, False)
            _write_switch(DC_FEEDIN, False)
        S["effective"] = "RELEASED"
        _publish(mode, "RELEASED", "Victron default: registers released, executor idle", None, None, None, ev_from_grid)
        return

    plan = None
    if soc is None:
        effective, reason = "SELF", "battery SoC unavailable -> self-consumption"
    elif mode == "Auto":
        plan, why = _plan_step(now)
        if plan is None:
            effective, reason = "SELF", f"Auto: {why} -> self-consumption"
        else:
            effective, reason = plan["mode"], f"Auto: plan {plan['mode']}" + (f" ({plan['summary'][0]})" if plan["summary"] else "")
            if effective == "CHARGE" and soc >= _num("input_number.ess_max_soc_grid_charge", 95):
                effective, reason = "HOLD", "Auto: plan CHARGE, but the grid-charge SoC limit is reached -> hold"
            spike = _num("input_number.ess_sell_spike_price", 0)
            sell_ok = _on("input_boolean.ess_allow_battery_export") or (
                spike > 0 and export_price is not None and export_price >= spike)  # spike exception
            if effective == "EXPORT" and (not sell_ok or soc <= plan["floor"] + 0.5):
                effective, reason = "SELF", "Auto: plan EXPORT, but selling is off or the floor is reached -> self-consumption"
    elif mode == "Self-consumption":
        effective, reason = "SELF", "manual self-consumption"
    elif mode == "Hold":
        effective, reason = "HOLD", "manual hold"
    elif mode == "Charge now":
        target = _num("input_number.ess_max_soc_grid_charge", 95)
        if soc >= target:
            effective, reason = "HOLD", f"charge target {target:.0f} % reached -> hold"
        else:
            effective, reason = "CHARGE", f"manual grid charge to {target:.0f} %"
    else:  # Export now
        floor_soc = max(min_soc, _num("input_number.ess_manual_export_floor", 40))
        if soc <= floor_soc + 0.5:
            S["export_stopped"] = True
        elif soc > floor_soc + 2:
            S["export_stopped"] = False
        if S["export_stopped"]:
            effective, reason = "SELF", f"export floor {floor_soc:.0f} % reached -> self-consumption"
        else:
            effective, reason = "EXPORT", f"manual export down to {floor_soc:.0f} %"

    if effective != "HOLD":
        S["hold_level"] = None

    dvcc = -1
    if effective == "SELF":
        setpoint, min_target = offset + ev_offset, min_soc
    elif effective == "SOLAR_EXPORT":
        # Sell PV now, charge later: block battery charging (DVCC 0); deficits are still covered by the battery.
        setpoint, min_target, dvcc = offset + ev_offset, min_soc, 0
    elif effective == "HOLD":
        level = max(min_soc, math.floor(soc) - 1)
        S["hold_level"] = max(S["hold_level"] or 0, level)  # ratchet up when PV raises the SoC
        # The grid covers the consumption (house + EV) that PV does not, so the battery stays idle; a PV surplus still
        # charges it. With setpoint 0 the battery fed the house and a charging EV until the 1 % hold margin was used
        # (2026-10-28); the min SoC stays as the backstop if HA stops updating the setpoint.
        if soc <= min_soc + 0.5:
            # At the ESS min SoC the GX does not discharge anyway, so the setpoint need not follow the consumption
            # (215 pointless writes in 11 h of such HOLD, 2026-11-03..12).
            setpoint, min_target = offset + ev_offset, S["hold_level"]
        else:
            consumption = max(_num("sensor.victron_total_ac_out_consumption", 0.0) or 0.0, ev_power)
            setpoint, min_target = offset + max(0.0, consumption - (_num("sensor.pv_power", 0.0) or 0.0)), S["hold_level"]
    elif effective == "CHARGE":
        if plan is not None:  # the planned grid import (house + battery charge); a grid-fed EV comes on top
            setpoint = min(plan["grid_w"] + ev_offset, _num("input_number.ess_max_import", 10000))
        else:
            power = min(_num("input_number.ess_manual_power", 5000), _num("input_number.ess_max_grid_charge", 9000))
            setpoint = min(power + ev_power, _num("input_number.ess_max_import", 10000))
        min_target = min_soc
    else:  # EXPORT: the ESS min SoC is the export floor, so the GX stops by itself if HA dies
        if plan is not None:
            floor_soc = max(min_soc, plan["floor"])
            setpoint = max(plan["grid_w"], -_num("input_number.ess_max_export", 10000)) + ev_offset
        else:
            floor_soc = max(min_soc, _num("input_number.ess_manual_export_floor", 40))
            power = min(_num("input_number.ess_manual_power", 5000), _num("input_number.ess_max_export", 10000))
            setpoint = -power + ev_offset  # a car fed from the grid must not be paid for by the battery export
        min_target = max(min_soc, min(floor_soc, math.floor(soc) - 1))

    # Clamp to the import/export limits and the register range: a 10.6 kW guest car pushed the SELF target above
    # the entity's 11000 W maximum and every write was rejected (2026-10-23). Above the import limit the battery
    # covers the rest, which protects the 16 A main fuse.
    sp_range = state.getattr(SP) or {}
    hi = min(_num("input_number.ess_max_import", 10000), float(sp_range.get("max", 11000)))
    lo = max(-_num("input_number.ess_max_export", 10000), float(sp_range.get("min", -11000)))
    setpoint = round(max(lo, min(hi, setpoint)))
    # A charging EV fluctuates by ±200 W; a wider deadband then avoids a setpoint write every ~30 s. HOLD follows the
    # house consumption, which moves more: 200 W gave ~19 writes/h (75 % of all setpoint writes), so it uses 400 W.
    _write_number(SP, setpoint, 400 if effective == "HOLD" else 200 if ev_power > 300 else 100)
    _write_number(MINSOC, min_target)
    _write_number(DVCC, dvcc)
    _write_switch(PREVENT_FEEDBACK, not solar_export)
    _write_switch(DC_FEEDIN, solar_export)

    if S["effective"] != effective:
        log.info(f"ess_executor: {S['effective']} -> {effective} ({reason}; trigger {trigger})")
    S["effective"] = effective
    _publish(mode, effective, reason, setpoint, min_target, solar_export, ev_from_grid)


def _publish(mode, effective, reason, setpoint, min_target, solar_export, ev_from_grid):
    last_write = max(S["writes"]) if S["writes"] else None
    state.set("sensor.ess_executor", effective, {
        "friendly_name": "ESS executor",
        "icon": "mdi:home-battery",
        "requested_mode": mode,
        "reason": reason,
        "setpoint_target_w": setpoint,
        "min_soc_target": min_target,
        "solar_export": solar_export,
        "ev_from_grid": ev_from_grid,
        "writes_last_hour": len(S["writes"]),
        "last_write": last_write.isoformat() if last_write else None,
        "override_until": _get("input_datetime.ess_override_until") if mode in MANUAL_MODES else None,
        "updated": dt_util.now().isoformat(),
        "instance": INSTANCE,
        "started": STARTED.isoformat(),
    })
