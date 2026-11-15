"""ESS optimizer: plans the house battery and EV charging per quarter and publishes sensor.ess_plan.

See docs/optimizer.md. In ess_mode Auto the executor follows this plan.
The dynamic programme itself is the native module /config/ess_lib/ess_dp.py (shared with the offline
backtest); this script only collects the inputs, runs it in a worker thread and publishes the result.
"""

import datetime as dt
import functools
import importlib
import math
import sys
import time

import homeassistant.util.dt as dt_util

LIB = "/config/ess_lib"
if LIB not in sys.path:
    sys.path.insert(0, LIB)
import ess_dp

ess_dp = importlib.reload(ess_dp)  # pick up a new ess_dp.py when this script is reloaded

QUARTER = dt.timedelta(minutes=15)
SOC = "sensor.victron_battery_soc_223"
FULL_CHARGE_AGE = "sensor.victron_battery_history_timesincelastfullcharge_223"
MODE_CODE = {"SELF": "S", "HOLD": "H", "CHARGE": "C", "EXPORT": "E", "SOLAR_EXPORT": "X"}
# SEK per EV charge start. Night prices differ by a few öre between quarters, which split the car's charge into
# 3-7 starts per night (2026-11-02..12); 0.15 SEK groups them into 1-2 blocks at no measurable cost.
EV_START_COST = 0.15


def _num(entity, default=None):
    try:
        return float(state.get(entity))
    except (TypeError, ValueError, NameError):
        return default


def _on(entity):
    try:
        return state.get(entity) == "on"
    except NameError:
        return False


def _attrs(entity):
    try:
        return state.getattr(entity) or {}
    except NameError:
        return {}


def _quarters(day):
    tz = dt_util.get_default_time_zone()
    start = dt.datetime.combine(day, dt.time(0), tzinfo=tz).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), tzinfo=tz).astimezone(dt.timezone.utc)
    out = []
    t = start
    while t < end:
        out.append(t.astimezone(tz))
        t += QUARTER
    return out


def _loc(t):
    return t.astimezone(dt_util.get_default_time_zone())


def _series(entity, names, today):
    """Concatenate day arrays of a forecast/price sensor into {quarter_start (UTC): value}. Keys are UTC because
    aware datetimes with the same zone compare by wall time: the repeated autumn DST hour would merge."""
    attrs = _attrs(entity)
    out = {}
    for offset, name in enumerate(names):
        values = attrs.get(name) or []
        for t, v in zip(_quarters(today + dt.timedelta(days=offset)), values):
            if v is not None:
                out[t.astimezone(dt.timezone.utc)] = float(v)
    return out


def _params():
    return ess_dp.Params(
        # Usable energy capacity: 29.2 kWh measured 2026-11-03..12 (SoC change vs shunt energy, 20 windows).
        capacity=_num("input_number.ess_battery_capacity", 29),
        ev_start_cost=EV_START_COST,
        min_soc=_num("input_number.ess_min_soc", 8),
        max_soc_grid=_num("input_number.ess_max_soc_grid_charge", 95),
        rte=_num("input_number.ess_round_trip_efficiency", 85) / 100,
        wear=_num("input_number.ess_wear_cost", 5) / 100,
        max_import=_num("input_number.ess_max_import", 10000) / 1000,
        max_grid_charge=_num("input_number.ess_max_grid_charge", 9000) / 1000,
        max_export=_num("input_number.ess_max_export", 10000) / 1000,
        allow_grid_charge=_on("input_boolean.ess_allow_grid_charge"),
        allow_batt_export=_on("input_boolean.ess_allow_battery_export"),
        allow_solar_export=_on("input_boolean.ess_allow_solar_export"),
        block_negative=_on("input_boolean.ess_block_export_negative_price"),
        allow_batt_to_ev=_on("input_boolean.ess_allow_battery_to_ev"),
        reserve_margin=_num("input_number.ess_night_reserve_margin", 20) / 100,
        reserve_pv_factor=_num("input_number.ess_reserve_solar_factor", 70) / 100,
        # Power-dependent losses measured 2026-10-17 (off = constant round-trip efficiency + standby loss helpers).
        eff_model="measured" if _on("input_boolean.ess_measured_efficiency") else "constant",
    )


def _blocks(times, modes, socs, ev):
    """Human-readable summary of the non-SELF blocks and EV charging."""
    names = {"HOLD": "Hold", "CHARGE": "Grid charge", "EXPORT": "Sell", "SOLAR_EXPORT": "Sell PV"}
    out = []
    t = 0
    while t < len(modes):
        m = modes[t]
        k = t
        while k + 1 < len(modes) and modes[k + 1] == m:
            k += 1
        if m != "SELF" and (k - t >= 1 or m in ("CHARGE", "EXPORT")):
            out.append(f"{names[m]} {_loc(times[t]).strftime('%a %H:%M')}–{_loc(times[k] + QUARTER).strftime('%H:%M')} → {socs[k]:.0f} %")
        t = k + 1
    evq = [i for i, v in enumerate(ev) if v > 0]
    if evq:
        out.append(f"EV {len(evq)} quarters {_loc(times[evq[0]]).strftime('%a %H:%M')}–{_loc(times[evq[-1]] + QUARTER).strftime('%H:%M')}")
    return out


@service  # also called by the HA automation "ESS Pyscript keepalive" when no plan is fresh (DST freeze)
@time_trigger("startup", "cron(1,16,31,46 * * * *)")
@state_trigger(
    "input_number.ess_min_soc", "input_number.ess_max_soc_grid_charge", "input_number.ess_round_trip_efficiency",
    "input_number.ess_wear_cost", "input_number.ess_max_import", "input_number.ess_max_grid_charge",
    "input_number.ess_max_export", "input_number.ess_night_reserve_margin", "input_number.ess_reserve_solar_factor",
    "input_boolean.ess_allow_grid_charge", "input_boolean.ess_allow_battery_export",
    "input_boolean.ess_allow_solar_export", "input_boolean.ess_block_export_negative_price",
    "input_boolean.ess_allow_battery_to_ev", "input_boolean.ess_force_full_charge", "input_select.ess_ev_mode",
    "input_number.ess_ev_opportunistic_target", "input_number.ess_ev_opportunistic_max_price",
    "input_boolean.ess_ev_guest_mode", "input_number.ess_standby_loss", "input_boolean.ess_ev_smart_control",
    "input_number.ess_sell_min_pv_today", "input_number.ess_sell_spike_price",
    "input_boolean.ess_measured_efficiency", "input_number.ess_battery_capacity",
    "sensor.ess_ev.at_home",  # replan at once when the car is plugged in or leaves
    "sensor.ess_ev.next_departure", "sensor.ess_ev.target",  # e.g. a manual departure set or changed
)
def ess_optimize(**kwargs):
    task.unique("ess_optimize")
    started = time.monotonic()
    now = dt_util.now()
    today = now.date()
    q_now = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0).astimezone(dt.timezone.utc)

    imp = _series("sensor.ess_import_price", ["today", "tomorrow"], today)
    exp = _series("sensor.ess_export_price", ["today", "tomorrow"], today)
    pv = _series("sensor.ess_pv_forecast", ["today", "tomorrow", "day_after"], today)
    load = _series("sensor.ess_load_forecast", ["today", "tomorrow", "day_after"], today)
    soc = _num(SOC)
    times = sorted([t for t in imp if t >= q_now and t in exp])
    ext = sorted([t for t in load if t >= q_now and t in pv])
    if soc is None or len(times) < 8 or len(ext) < len(times):
        state.set("sensor.ess_plan", "unavailable", {"friendly_name": "ESS plan", "icon": "mdi:chart-timeline",
                                                     "error": "missing prices, forecasts or SoC", "computed_at": now.isoformat()})
        return
    p = _params()
    # Owner's rule (2026-10-08): never sell before tomorrow's prices are published, i.e. while the horizon
    # ends at midnight (00:00 to ~13:00). No battery export and no PV export instead of charging the battery.
    tomorrow_known = len([t for t in times if _loc(t).date() > today]) > 0
    # Owner's rule (2026-10-09): sell only after a productive solar day, i.e. today's PV >= the slider value
    # (0 = off). A cloudy day means the battery will not be refilled for free, so stored energy is kept.
    pv_today = _num("sensor.pv_energy_today", 0.0)
    pv_min = _num("input_number.ess_sell_min_pv_today", 10)
    pv_ok = pv_min <= 0 or pv_today >= pv_min
    notes = []
    if not p.allow_batt_export:
        notes.append("selling is switched off")
    if not tomorrow_known:
        notes.append("tomorrow's prices are not published yet")
    if not pv_ok:
        notes.append(f"solar today {pv_today:.1f} of {pv_min:.0f} kWh")
    normal_ok = p.allow_batt_export and tomorrow_known and pv_ok
    # Spike exception (owner, 2026-10-17): in any quarter whose export price reaches ess_sell_spike_price
    # (0 = off), selling is allowed regardless of the rules above. Its reserve counts cheap grid quarters as
    # refill points (ess_dp.export_floor_grid), since a spike sale is refilled from the grid, not the sun.
    spike = _num("input_number.ess_sell_spike_price", 0)
    a_exp_all = [exp[t] for t in times]
    spike_mask = [spike > 0 and not normal_ok and x >= spike for x in a_exp_all]
    n_spike = len([x for x in spike_mask if x])
    p.allow_batt_export = True
    p.sell_mask = [normal_ok or x for x in spike_mask]
    p.spike_mask = spike_mask if n_spike else None
    # Quarters outside sell_mask may neither sell nor export PV by choice (ess_dp absorbs the surplus there).
    if n_spike:
        notes.append(f"spike exception: selling allowed in {n_spike} quarters at >= {spike:.2f} SEK/kWh")

    # Battery sync: require 100 % by 06:00 when a full charge is due. The target stays until 06:00 itself; it used
    # to need 2 h of lead time, so every re-plan from 04:15 dropped it and the sync never finished (2026-10-22..24).
    full_due = _on("input_boolean.ess_force_full_charge") or (
        _num(FULL_CHARGE_AGE, 0) / 86400 >= _num("input_number.ess_full_charge_interval", 14))
    if full_due:
        for i, t in enumerate(times):
            if _loc(t).hour == 6 and t.minute == 0 and i >= 1:
                p.full_charge_index = i - 1
                break
        if p.full_charge_index < 0 and len(times) > 1:
            # 06:00 has passed without a full battery (e.g. the DST night, 2026-10-25) and tomorrow's 06:00 is not in
            # the horizon yet: be full by the horizon end instead, in its cheapest quarters.
            p.full_charge_index = len(times) - 1

    # EV inputs from the executor's EV status.
    ev_attr = _attrs("sensor.ess_ev")
    ev_mode = state.get("input_select.ess_ev_mode")
    ev_need = opp_kwh = 0.0
    ev_deadline = 0
    ev_q = (ev_attr.get("charge_power_w") or 3600) / 1000 * ess_dp.DT
    ev_fixed = None
    guest = state.get("input_boolean.ess_ev_guest_mode") == "on"  # a guest car is fed from the grid: not planned
    # The EV is planned only under smart EV control; otherwise the car's own timer decides (passive).
    smart_ev = state.get("input_boolean.ess_ev_smart_control") == "on"
    if ev_attr.get("at_home") and ev_mode != "Off" and not guest and smart_ev:
        kwh_pct = ev_attr.get("kwh_per_percent") or 0.27
        ev_soc = ev_attr.get("soc") or 0.0
        target = ev_attr.get("target") or 0.0
        if ev_mode == "Charge now":
            need = max(0.0, 100 - ev_soc) * kwh_pct
            n_q = math.ceil(need / ev_q) if ev_q > 0 else 0
            ev_fixed = [ev_q if i < n_q else 0.0 for i in range(len(ext))]
        else:
            dep = ev_attr.get("next_departure")
            if dep and ev_attr.get("needed_kwh"):
                deadline = dt_util.parse_datetime(dep) - dt.timedelta(minutes=_num("input_number.ess_ev_finish_margin", 30))
                ev_deadline = len([t for t in times if t + QUARTER <= deadline])
                if deadline <= times[-1] + QUARTER:  # deadline within the price horizon; otherwise plan later
                    ev_need = ev_attr.get("needed_kwh")
            opp_target = _num("input_number.ess_ev_opportunistic_target", 80)
            opp_kwh = max(0.0, opp_target - max(ev_soc, target if ev_need else 0.0)) * kwh_pct

    # Arrays: horizon = price quarters; load/PV continue beyond it for the night reserve.
    a_imp = [imp[t] for t in times]
    a_exp = [exp[t] for t in times]
    # The load forecast (factor already applied) misses the inverter's own standby/conversion loss, which
    # the consumption sensors do not measure (calibrated 2026-10-04 against the real SoC curve: ~40 W).
    # The measured efficiency model already contains it.
    loss = 0.0 if p.eff_model == "measured" else _num("input_number.ess_standby_loss", 40) / 1000 * ess_dp.DT
    a_load = [load[t] + loss for t in ext]
    a_pv = [pv[t] for t in ext]
    fn = functools.partial(ess_dp.plan_with_ev, p, soc, a_load, a_pv, a_imp, a_exp, ev_need=ev_need, ev_q=ev_q,
                           ev_deadline=ev_deadline, opp_kwh=opp_kwh,
                           opp_max_price=_num("input_number.ess_ev_opportunistic_max_price", 180) / 100,
                           ev_fixed=ev_fixed, ev_charging_now=bool(ev_attr.get("charging")))
    plan = await hass.async_add_executor_job(fn)

    modes = plan["mode"]
    T = len(times)
    ev = plan["ev"]
    first = modes[0]
    setpoint = round(plan["grid"][0] / ess_dp.DT * 1000) if first in ("CHARGE", "EXPORT") else 0
    state.set("sensor.ess_plan", first, {
        "friendly_name": "ESS plan",
        "icon": "mdi:chart-timeline",
        "shadow": state.get("input_select.ess_mode") != "Auto",
        "start": _loc(times[0]).isoformat(),
        "quarters": T,
        "modes": "".join([MODE_CODE[m] for m in modes]),
        "soc": [round(x) for x in plan["soc"]],
        "batt_kw": [round(x / ess_dp.DT, 1) for x in plan["batt"]],
        "grid_kw": [round(x / ess_dp.DT, 1) for x in plan["grid"]],
        "ev_kw": [round(x / ess_dp.DT, 1) for x in ev],
        # Forecasts, for the EV chart's source split (solar / battery / grid).
        "pv_kw": [round(a_pv[i] / ess_dp.DT, 2) for i in range(T)],
        "load_kw": [round(a_load[i] / ess_dp.DT, 2) for i in range(T)],
        "floor": [round(x) for x in plan["floor"]],
        "setpoint_w": setpoint,
        "floor_now": round(plan["floor"][0]),
        "summary": _blocks(times, modes, plan["soc"], ev),
        "cost": round(plan["net_cost"], 2),
        "baseline_cost": round(plan["baseline_net_cost"], 2),
        "savings": round(plan["savings"], 2),
        "ev_need_kwh": round(ev_need, 2),
        "ev_planned_kwh": round(sum(ev), 2),
        "ev_feasible": plan["ev_feasible"],
        "full_charge_due": full_due,
        "eff_model": p.eff_model,
        "capacity_kwh": p.capacity,
        "selling_allowed": normal_ok,
        "spike_quarters": n_spike,
        "selling_note": "; ".join(notes) if notes else None,
        "pv_today_kwh": round(pv_today, 1),
        "soc_start": soc,
        "horizon_end": _loc(times[-1] + QUARTER).isoformat(),
        "computed_at": now.isoformat(),
        "runtime_s": round(time.monotonic() - started, 2),
    })
