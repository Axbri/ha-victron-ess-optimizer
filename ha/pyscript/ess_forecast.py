"""ESS forecasts: PV and house-load forecasts per quarter, EV charging learning, forecast accuracy.

See docs/forecasts.md. Forecast attributes `today` / `tomorrow` / `day_after` are kWh per
quarter from local midnight, like the price sensors (92/96/100 quarters on DST days).

  sensor.ess_pv_forecast        the PV forecast the optimizer uses, source input_select.ess_pv_source:
                                Open-Meteo Solar Forecast (roof + wall, quarters) x slider, or VRM (below);
                                VRM is the fallback when Open-Meteo has no complete data
  sensor.ess_pv_forecast_vrm    VRM hourly solar forecast -> quarters, x sunny-day bias (sunny days only) x slider
                                Snow compensation (below input_number.ess_pv_snow_temp): sensor.ess_pv_forecast is
                                scaled by measured production / forecast of today's elapsed quarters
  sensor.ess_load_forecast      house load excl. EV charging: hour-of-day profile of the last 14 days,
                                recency-weighted (half-life 4 days), temperature-normalised and re-applied
                                with the forecast temperature (heating W = coeff x (balance - outdoor temp))
  sensor.ess_forecast_accuracy  daily forecast vs actual; state = PV bias factor
  sensor.ess_ev_learning        learned EV grid kWh per % SoC and charge power
"""

import datetime as dt
import statistics as pystats

import homeassistant.util.dt as dt_util

CONSUMPTION = ["sensor.victron_system_consumption_l1", "sensor.victron_system_consumption_l2",
               "sensor.victron_system_consumption_l3"]
EV_POWER = "sensor.victronevcharger_total_power40"
EV_ENERGY = "sensor.victronevcharger_energy_forward40"  # per-session counter
EV_STATUS = "sensor.victronevcharger_status40"
CAR_SOC = "sensor.ev_soc"
PV_ENERGY = "sensor.pv_energy_total"
OUTDOOR_TEMP = "sensor.outdoor_temperature"
WEATHER = "weather.forecast_home"
# The outdoor sensor (which the heating coefficient was fitted on) reads 0.4 °C above met.no on average
# (10 days to 2026-10-19: +1.0 at night, -0.6 in the afternoon); added to forecast temperatures.
SENSOR_VS_FORECAST = 0.4
L1_EV = 2000  # W: an L1 hour above this without EVCS data is the car charging while the EVCS was offline
WORKDAY = "binary_sensor.workday_sensor"
# PV forecast source (owner, 2026-11-12): Open-Meteo beat bias-corrected VRM over 23 days of parallel running
# (day-ahead MAE 1.91 vs 2.67 kWh/day, same-day 1.63 vs 2.57; sensor.ess_pv_compare) and needs no bias correction.
PV_SOURCE = "input_select.ess_pv_source"  # "Open-Meteo" or "VRM"
OM_PREFIX = ["sensor.om_solar_roof_energy_production_", "sensor.om_solar_wall_energy_production_"]
OM_DAY = {"today": "today", "tomorrow": "tomorrow", "day_after": "d2"}
DAYS = (("today", 0), ("tomorrow", 1), ("day_after", 2))
HISTORY = "pyscript.ess_forecast_history"
# Snow compensation (owner, 2026-11-21): a thin snow layer cut production to 15-25 % of the forecast on 2026-11-19..21
# while the forecasts saw clear sky. Below SNOW_TEMP (outdoor) the forecast in use is scaled by the measured ratio
# actual / forecast of today's elapsed quarters, weighted towards the last hours so it follows the snow sliding off.
SNOW_ON = "input_boolean.ess_pv_snow_compensation"
SNOW_TEMP = "input_number.ess_pv_snow_temp"
SNOW_STATE = "pyscript.ess_pv_snow"  # last measured factor, and the lowest applied factor per day (snow days)
SNOW_HALF_LIFE_H = 2.0  # recency weighting of the elapsed quarters
SNOW_MIN_FC_KWH = 0.5  # forecast energy in today's elapsed quarters before today's own ratio is trusted
SNOW_MIN_WFC_KWH = 0.2  # the same, recency weighted (the evening has no recent forecast to compare with)
SNOW_CARRY_H = 36  # a measured factor is carried into the next morning (and a cold day after) for this long
SNOW_DAY_FLAG = 0.85  # a day whose applied factor went below this is a snow day: skipped by the VRM bias learning
EV_LEARN = "pyscript.ess_ev_learning"
PROFILE_DAYS = 14  # workday profile: workdays of the last 14 days
HALF_LIFE_DAYS = 4
FREE_DAYS = 28  # weekend/holiday profile: free days of the last 28 days
FREE_HALF_LIFE_DAYS = 7
QUARTER = dt.timedelta(minutes=15)

# Two sessions measured on 2026-09-29 and 2026-10-02 (EVCS kWh / EV SoC gain).
EV_SEED = [
    {"start": "2026-09-29T05:20", "kwh": 5.61, "soc_from": 63.0, "soc_to": 82.7, "kwh_per_pct": 0.285, "power_w": 3650},
    {"start": "2026-10-02T05:30", "kwh": 5.25, "soc_from": 59.8, "soc_to": 80.9, "kwh_per_pct": 0.249, "power_w": 3650},
]

C = {"workday": {}, "session": None}

state.persist(HISTORY, default_value="0", default_attributes={"days": [], "snapshot": {}})
state.persist(EV_LEARN, default_value="0", default_attributes={"sessions": EV_SEED})
# Seeded with the snow days before this existed (measured day ratios), so they stay out of the bias learning.
state.persist(SNOW_STATE, default_value="1.0", default_attributes={
    "factor": 1.0, "measured_at": None, "min_by_day": {"2026-11-19": 0.21, "2026-11-20": 0.17, "2026-11-21": 0.2}})


def _num(entity, default=None):
    try:
        return float(state.get(entity))
    except (TypeError, ValueError, NameError):
        return default


def _tz():
    return dt_util.get_default_time_zone()


def _midnight(day):
    return dt.datetime.combine(day, dt.time(0), tzinfo=_tz())


def _quarters(day):
    """Quarter start times (local, aware) of a local calendar day; handles DST days."""
    start = _midnight(day).astimezone(dt.timezone.utc)
    end = _midnight(day + dt.timedelta(days=1)).astimezone(dt.timezone.utc)
    out = []
    t = start
    while t < end:
        out.append(t.astimezone(_tz()))
        t += QUARTER
    return out


def _is_workday(day):
    key = day.isoformat()
    if key not in C["workday"]:
        resp = hass.services.async_call("workday", "check_date", {"entity_id": WORKDAY, "check_date": key},
                                        blocking=True, return_response=True)
        C["workday"][key] = bool(resp.get(WORKDAY, {}).get("workday"))
    return C["workday"][key]


def _vrm_store():
    for entry in hass.config_entries.async_entries("victron_remote_monitoring"):
        data = getattr(getattr(entry, "runtime_data", None), "data", None)
        if data is not None:
            return data
    return None


def _quarter_kwh(records, t):
    """kWh in the quarter starting at t, from hourly Wh records keyed by hour-start unix time.

    Power is interpolated towards the neighbouring hours, then scaled so the hour keeps its energy.
    """
    ts = int(t.timestamp())
    hour = ts - ts % 3600
    wh = records.get(hour)
    if wh is None:
        return None
    prev_wh, next_wh = records.get(hour - 3600, wh), records.get(hour + 3600, wh)
    rates = []
    for off in (-0.375, -0.125, 0.125, 0.375):
        nb = prev_wh if off < 0 else next_wh
        rates.append(max(0.0, wh + (nb - wh) * abs(off)))
    total = sum(rates)
    if total <= 0:
        return 0.0
    return wh * rates[(ts - hour) // 900] / total / 1000


def _statistics(ids, start, end, types, period="hour"):
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, set(ids), period, None, set(types))


def _base_load_hours(rows):
    """unix hour start -> house load excl. EV (W), for hours where all three phases have data."""
    per_phase = []
    for eid in CONSUMPTION:
        per_phase.append({int(r["start"]): r["mean"] for r in rows.get(eid, []) if r.get("mean") is not None})
    ev = {int(r["start"]): r["mean"] or 0.0 for r in rows.get(EV_POWER, []) if r.get("mean") is not None}
    l1_typ = sorted([v for v in per_phase[0].values() if v <= L1_EV] or [150.0])
    l1_typ = l1_typ[len(l1_typ) // 2]
    out = {}
    for ts in per_phase[0]:
        if ts in per_phase[1] and ts in per_phase[2]:
            l1 = per_phase[0][ts]
            if ts not in ev and l1 > L1_EV:
                l1 = l1_typ  # hidden EV charging (EVCS offline)
            out[ts] = max(0.0, l1 + per_phase[1][ts] + per_phase[2][ts] - ev.get(ts, 0.0))
    return out


def _heat(temp, coeff, balance):
    """Temperature-dependent part of the house load (W). Fitted 2026-10-19 on hourly statistics 2024-01..2026-10
    (EV removed): 70 W per °C below 9 °C cut the day-ahead daily error from 5.3 to 3.9 kWh RMSE (4.3 with a
    ±1.5 °C forecast error); a second, steeper slope below 0 °C added nothing."""
    if temp is None or coeff <= 0:
        return 0.0
    return coeff * max(0.0, balance - temp)


def _temp_coeff():
    return _num("input_number.ess_load_temp_coeff", 70), _num("input_number.ess_load_temp_balance", 9)


def _temp_forecast():
    """unix hour start -> outdoor temperature (sensor scale): met.no hourly (48 h), else daily mean of high/low."""
    out = {}
    for typ in ("daily", "hourly"):  # hourly overwrites daily where both exist
        try:
            resp = hass.services.async_call("weather", "get_forecasts", {"entity_id": WEATHER, "type": typ},
                                            blocking=True, return_response=True)
            items = resp[WEATHER]["forecast"]
        except Exception as err:
            log.warning(f"ess_forecast: weather {typ} forecast failed: {err}")
            continue
        for it in items:
            start = dt_util.parse_datetime(it.get("datetime") or "")
            temp = it.get("temperature")
            if start is None or temp is None:
                continue
            if typ == "daily":
                low = it.get("templow")
                mean = (temp + low) / 2 if low is not None else temp
                h0 = int(_midnight(dt_util.as_local(start).date()).timestamp())
                for k in range(25):
                    out[h0 + 3600 * k] = mean + SENSOR_VS_FORECAST
            else:
                out[int(start.timestamp()) // 3600 * 3600] = temp + SENSOR_VS_FORECAST
    return out


def _profile(hours, end_day, temps=None, coeff=0.0, balance=0.0):
    """Hour-of-day load (W) per day class {workday: True, free day: False}, recency-weighted.

    With coeff > 0 the heating part (_heat) is removed per hour first, so the profiles are the load at or
    above the balance temperature and the season no longer shifts them. Backtest 2024-02..2026-10
    (2026-10-19), day-ahead daily RMSE: one profile 5.35 kWh; + temperature 3.87; + class split 3.85
    (autumn 2026: 4.33 -> 2.80). Without the temperature correction the split lagged the autumn load
    increase (evaluated 2026-10-16), so with coeff = 0 one common profile is used."""
    split = coeff > 0
    windows = {True: (PROFILE_DAYS, HALF_LIFE_DAYS), False: (FREE_DAYS, FREE_HALF_LIFE_DAYS) if split else (PROFILE_DAYS, HALF_LIFE_DAYS)}
    end = _midnight(end_day).timestamp()
    out, info = {}, {}
    for cls, (n_days, half_life) in windows.items():
        start = _midnight(end_day - dt.timedelta(days=n_days)).timestamp()
        sums, weights, days = {}, {}, set()
        t_sum = t_w = 0.0
        for ts, w in hours.items():
            if not start <= ts < end:
                continue
            local = dt.datetime.fromtimestamp(ts, _tz())
            if split and _is_workday(local.date()) != cls:
                continue
            temp = (temps or {}).get(ts)
            if coeff > 0:
                if temp is None:
                    continue
                w = w - _heat(temp, coeff, balance)
            age = (end_day - local.date()).days
            wt = 0.5 ** (age / half_life)
            sums[local.hour] = sums.get(local.hour, 0.0) + w * wt
            weights[local.hour] = weights.get(local.hour, 0.0) + wt
            days.add(local.date())
            if temp is not None:
                t_sum += temp * wt
                t_w += wt
        overall = sum(sums.values()) / sum(weights.values()) if weights else 500.0
        out[cls] = [sums[h] / weights[h] if weights.get(h) else overall for h in range(24)]
        info["workday" if cls else "free_day"] = {"days": len(days), "half_life_days": half_life,
                                                  "temp_mean_c": round(t_sum / t_w, 1) if t_w else None}
    return out, info


def _day_load(profile, day, factor, temps=None, coeff=0.0, balance=0.0):
    cls = _is_workday(day)
    return [(profile[cls][t.hour] + _heat((temps or {}).get(int(t.timestamp()) // 3600 * 3600), coeff, balance))
            / 1000 * 0.25 * factor for t in _quarters(day)]


def _history():
    try:
        return dict(state.getattr(HISTORY) or {})
    except NameError:
        return {"days": [], "snapshot": {}}


def _attrs(entity):
    try:
        return state.getattr(entity) or {}
    except NameError:
        return {}


def _om_day(day, suffix):
    """Open-Meteo forecast (roof + wall) as kWh per quarter of a local day. None when an array has no data for
    that day (integration down, or it retained an older forecast), so the caller can fall back to VRM."""
    quarters = [int(t.timestamp()) for t in _quarters(day)]
    total = {}
    for prefix in OM_PREFIX:
        per = {}
        for k, v in (_attrs(prefix + suffix).get("wh_period_15m") or {}).items():
            t = dt_util.parse_datetime(k)
            if t is not None and v is not None:
                per[int(t.timestamp()) // 900 * 900] = float(v)
        if len([q for q in quarters if q in per]) < len(quarters) - 8:
            return None
        for q, v in per.items():
            total[q] = total.get(q, 0.0) + v
    return [total[q] / 1000 if q in total else None for q in quarters]


def _clear_ref(days, raw_totals):
    """Clear-sky reference (kWh/day): the highest raw VRM daily forecast in the last 14 days of history and
    in the current forecast. It follows the season, since recent sunny days set it."""
    recent = [d.get("pv_fc_raw") or 0.0 for d in days if not d.get("backtest")][-14:]
    return max(recent + list(raw_totals) + [1.0])


def _sun_weight(clearness):
    """0 on cloudy days, 1 on sunny days: full weight at the sunny threshold, ramping up from 20 points below."""
    thr = _num("input_number.ess_pv_sunny_threshold", 60) / 100
    return min(1.0, max(0.0, (clearness - (thr - 0.2)) / 0.2))


def _snow_days():
    """Dates (ISO) whose applied snow factor went below SNOW_DAY_FLAG."""
    by_day = _attrs(SNOW_STATE).get("min_by_day") or {}
    return [k for k, v in by_day.items() if v is not None and v < SNOW_DAY_FLAG]


def _pv_bias(days, ref):
    """Actual / raw VRM forecast, learned from sunny days only (VRM is low on clear days and high on cloudy
    ones, so a mixed average is wrong on both). Snow days are skipped: their low production is not a VRM error."""
    snow = _snow_days()
    sunny = [d for d in days if d.get("pv_act") is not None and (d.get("pv_fc_raw") or 0) > 1
             and not d.get("snow") and d.get("date") not in snow
             and _sun_weight(d.get("clearness") or d["pv_fc_raw"] / ref) >= 1.0][-7:]
    if len(sunny) < 3:
        return 1.0, len(sunny)
    ratio = sum([d["pv_act"] for d in sunny]) / sum([d["pv_fc_raw"] for d in sunny])
    return min(1.8, max(0.5, ratio)), len(sunny)  # cap 1.5 until 2026-11-12: it was reached (ratios 1.45-1.59)


def _pv_attrs(raws, source, bias, bias_days, ref, factor):
    """(remaining kWh today, attributes) of a PV forecast sensor from raw kWh-per-quarter lists per day."""
    now = dt_util.now()
    today = now.date()
    raw_totals = {name: sum([v or 0.0 for v in r]) for name, r in raws.items()}
    attrs = {"unit_of_measurement": "kWh", "friendly_name": "ESS PV forecast", "icon": "mdi:solar-power-variant",
             "start": _midnight(today).isoformat(), "bias": round(bias, 3), "bias_days": bias_days,
             "clear_ref_kwh": round(ref, 1), "factor": factor, "source": source}
    remaining = 0.0
    for name, offset in DAYS:
        raw = raws[name]
        clearness = raw_totals[name] / ref
        applied = 1 + (bias - 1) * _sun_weight(clearness)  # the sunny-day correction only on sunny days
        values = [round((v or 0.0) * applied * factor, 4) for v in raw]
        attrs[name] = values
        attrs[f"{name}_kwh"] = round(sum(values), 2)
        attrs[f"{name}_raw_kwh"] = round(raw_totals[name], 2)
        attrs[f"{name}_clearness"] = round(clearness, 2)
        attrs[f"{name}_bias_applied"] = round(applied, 3)
        attrs[f"{name}_missing_quarters"] = sum([1 for v in raw if v is None])
        if offset == 0:
            remaining = sum([v for t, v in zip(_quarters(today), values) if t + QUARTER > now])
    attrs["raw_today_kwh"] = round(raw_totals["today"], 2)
    return remaining, attrs


def _snow_ratio(today_values):
    """(ratio, forecast kWh, actual kWh) over today's elapsed quarters: measured production / forecast, each quarter
    weighted 0.5 ** (age / SNOW_HALF_LIFE_H). Ratio None while there is too little forecast energy to judge."""
    now = dt_util.now()
    today = now.date()
    rows = _statistics([PV_ENERGY], _midnight(today), now, {"change"}, "5minute")
    act = {}
    starts = []
    for r in rows.get(PV_ENERGY, []):
        t0 = int(r["start"])
        starts.append(t0)
        q = t0 // 900 * 900
        act[q] = act.get(q, 0.0) + max(r.get("change") or 0.0, 0.0)
    if len(starts) < 2:
        return None, 0.0, 0.0
    # Only quarters whose three 5-minute rows are complete; the newest row is skipped (recorder commit lag).
    data_end = max(starts)
    f_sum = a_sum = wf = wa = 0.0
    for t, f in zip(_quarters(today), today_values):
        q = int(t.timestamp())
        if q + 900 > data_end:
            break
        a = act.get(q, 0.0)
        w = 0.5 ** ((now.timestamp() - q - 900) / 3600 / SNOW_HALF_LIFE_H)
        f_sum += f
        a_sum += a
        wf += w * f
        wa += w * a
    if f_sum < SNOW_MIN_FC_KWH or wf < SNOW_MIN_WFC_KWH:
        return None, f_sum, a_sum
    return min(1.0, max(0.0, wa / wf)), f_sum, a_sum


def _snow(attrs):
    """Scale the chosen PV forecast for snow on the panels (in place). Below SNOW_TEMP outdoor, today's factor is the
    measured ratio (_snow_ratio); before enough of today has passed, the last measured factor (up to SNOW_CARRY_H
    old). Tomorrow and the day after get the same factor when their forecast mean temperature is below SNOW_TEMP too.
    At or above SNOW_TEMP, or when switched off, nothing is scaled and the carried factor is cleared.
    Returns the remaining kWh of today."""
    now = dt_util.now()
    today = now.date()
    st = _attrs(SNOW_STATE)
    by_day = dict(st.get("min_by_day") or {})
    carried, carried_at = st.get("factor"), _parse_local(st.get("measured_at"))
    temp, thr = _num(OUTDOOR_TEMP), _num(SNOW_TEMP, 3.0)
    try:
        enabled = state.get(SNOW_ON) == "on"
    except NameError:
        enabled = False
    factor, active, note = 1.0, False, None
    measured = None
    if not enabled:
        note = "off"
    elif temp is None:
        note = "no outdoor temperature"
    elif temp >= thr:
        note = f"outdoor {temp:.1f} °C >= {thr:.1f} °C"
        carried, carried_at = 1.0, None
    else:
        active = True
        try:
            ratio, f_sum, a_sum = _snow_ratio(attrs["today"])
        except Exception as err:  # statistics unavailable: never block the forecast update
            log.warning(f"ess_forecast: snow ratio failed: {err}")
            ratio = None
        if ratio is not None:
            factor, measured = ratio, now
            note = f"measured today: {a_sum:.2f} of {f_sum:.2f} kWh forecast so far (recent hours weighted)"
            carried, carried_at = ratio, now
        elif carried is not None and carried_at is not None and (now - carried_at).total_seconds() < SNOW_CARRY_H * 3600:
            factor = float(carried)
            note = f"carried from {carried_at.strftime('%a %H:%M')} (too little forecast so far today to measure)"
        else:
            note = "no measurement yet"
    temps = _attrs("sensor.ess_load_forecast")
    day_f = {}
    for name, offset in DAYS:
        if offset == 0:
            day_f[name] = factor
            continue
        t = temps.get(f"{name}_temp_c")
        t = temp if t is None else t
        day_f[name] = factor if (active and t is not None and t < thr) else 1.0
    for name, offset in DAYS:
        f = day_f[name]
        attrs[f"{name}_snow_factor"] = round(f, 3)
        if f < 0.999:
            values = [round(v * f, 4) for v in attrs[name]]
            attrs[name] = values
            attrs[f"{name}_kwh"] = round(sum(values), 2)
    attrs["snow"] = {"active": active, "factor": round(factor, 3), "note": note, "outdoor_c": temp, "threshold_c": thr}
    if measured is not None:  # only measured factors mark a snow day (a carried morning value may be stale)
        key = today.isoformat()
        by_day[key] = round(min(by_day.get(key, 1.0), factor), 3)
    keep = (today - dt.timedelta(days=120)).isoformat()
    by_day = {k: v for k, v in by_day.items() if k >= keep}
    state.set(SNOW_STATE, round(factor, 3), {"factor": round(float(carried), 3) if carried is not None else 1.0,
                                             "measured_at": carried_at.isoformat() if carried_at else None,
                                             "last_measured": measured.isoformat() if measured else st.get("last_measured"),
                                             "min_by_day": by_day})
    return sum([v for t, v in zip(_quarters(today), attrs["today"]) if t + QUARTER > now])


def _parse_local(text):
    if not text:
        return None
    try:
        return dt_util.parse_datetime(text)
    except (TypeError, ValueError):
        return None


def _update_pv():
    today = dt_util.now().date()
    days_hist = _history().get("days", [])
    factor = _num("input_number.ess_solar_forecast_factor", 100) / 100

    # VRM with the sunny-day bias: always published (comparison, bias learning, fallback).
    vrm = None
    store = _vrm_store()
    if store is None:
        log.warning("ess_forecast: VRM forecast not available")
    else:
        records = {int(ts): float(wh) for ts, wh in store.solar.records}
        raws = {}
        for name, offset in DAYS:
            raws[name] = [_quarter_kwh(records, t) for t in _quarters(today + dt.timedelta(days=offset))]
        totals = [sum([v or 0.0 for v in r]) for r in raws.values()]
        ref = _clear_ref(days_hist, totals)
        bias, bias_days = _pv_bias(days_hist, ref)
        vrm = _pv_attrs(raws, "VRM", bias, bias_days, ref, factor)
        vrm_attrs = dict(vrm[1])
        vrm_attrs["friendly_name"] = "ESS PV forecast (VRM)"
        state.set("sensor.ess_pv_forecast_vrm", round(vrm[0], 2), vrm_attrs)

    # Open-Meteo: used as it is (bias over 23 days +0.1..+0.3 kWh/day). Clearness is relative to the best actual
    # day of the last 14 days and is informative only.
    om = None
    try:
        want_om = state.get(PV_SOURCE) == "Open-Meteo"
    except NameError:
        want_om = False
    if want_om:
        raws = {}
        for name, offset in DAYS:
            raws[name] = _om_day(today + dt.timedelta(days=offset), OM_DAY[name])
        if len([r for r in raws.values() if r is None]) > 0:
            log.warning("ess_forecast: Open-Meteo forecast incomplete, using VRM")
        else:
            totals = [sum([v or 0.0 for v in r]) for r in raws.values()]
            recent = [d.get("pv_act") or 0.0 for d in days_hist if not d.get("backtest")][-14:]
            om = _pv_attrs(raws, "Open-Meteo", 1.0, 0, max(recent + totals + [1.0]), factor)

    chosen = om or vrm
    if chosen is None:
        return None
    attrs = dict(chosen[1])
    if want_om and om is None:
        attrs["source"] = "VRM (Open-Meteo unavailable)"
    remaining = _snow(attrs)
    state.set("sensor.ess_pv_forecast", round(remaining, 2), attrs)
    return attrs["raw_today_kwh"]


def _update_load():
    now = dt_util.now()
    today = now.date()
    start = _midnight(today - dt.timedelta(days=max(PROFILE_DAYS + 8, FREE_DAYS + 1)))
    rows = _statistics(CONSUMPTION + [EV_POWER, OUTDOOR_TEMP], start, now, {"mean"})
    hours = _base_load_hours(rows)
    temps = {int(r["start"]): r["mean"] for r in rows.get(OUTDOOR_TEMP, []) if r.get("mean") is not None}
    coeff, balance = _temp_coeff()
    fc_temps = {}
    if coeff > 0:
        fc_temps = _temp_forecast()
        fallback = _num(OUTDOOR_TEMP)
        lo = int(_midnight(today).timestamp())
        hi = int(_midnight(today + dt.timedelta(days=3)).timestamp())
        for ts in range(lo, hi, 3600):
            if ts in temps and ts < now.timestamp() - 3600:
                fc_temps[ts] = temps[ts]  # past hours of today: measured
            elif ts not in fc_temps and fallback is not None:
                fc_temps[ts] = fallback
    profile, days = _profile(hours, today, temps, coeff, balance)
    factor = _num("input_number.ess_load_forecast_factor", 100) / 100
    attrs = {"unit_of_measurement": "kWh", "friendly_name": "ESS load forecast", "icon": "mdi:home-lightning-bolt",
             "start": _midnight(today).isoformat(), "factor": factor, "days_used": days,
             "profile_w": [round(w) for w in profile[True]],
             "profile_free_day_w": [round(w) for w in profile[False]],
             "temp_coeff_w_per_c": coeff, "temp_balance_c": balance,
             "note": "house load excl. EV charging" + ("; profile_w = load at/above the balance temperature" if coeff > 0 else "")}
    today_h = None
    remaining = 0.0
    for name, offset in (("today", 0), ("tomorrow", 1), ("day_after", 2)):
        day = today + dt.timedelta(days=offset)
        values = [round(v, 4) for v in _day_load(profile, day, factor, fc_temps, coeff, balance)]
        attrs[name] = values
        attrs[f"{name}_kwh"] = round(sum(values), 2)
        attrs[f"{name}_workday"] = _is_workday(day)
        if coeff > 0:
            d_lo, d_hi = int(_midnight(day).timestamp()), int(_midnight(day + dt.timedelta(days=1)).timestamp())
            day_t = [v for ts, v in fc_temps.items() if d_lo <= ts < d_hi]
            attrs[f"{name}_temp_c"] = round(sum(day_t) / len(day_t), 1) if day_t else None
            attrs[f"{name}_heating_kwh"] = round(sum([_heat(v, coeff, balance) for v in day_t]) / 1000 * factor, 2)
        if offset == 0:
            hourly = {}
            for t, v in zip(_quarters(day), values):
                hourly[t.hour] = hourly.get(t.hour, 0.0) + v * 1000
            today_h = [hourly.get(h, 0.0) for h in range(24)]
        if offset == 0:
            remaining = sum([v for t, v in zip(_quarters(day), values) if t + QUARTER > now])
    store = _vrm_store()
    if store is not None:  # VRM's consumption forecast includes EV charging: cross-check only
        recs = {int(ts): float(wh) for ts, wh in store.consumption.records}
        for name, offset in (("today", 0), ("tomorrow", 1)):
            day = today + dt.timedelta(days=offset)
            lo, hi = _midnight(day).timestamp(), _midnight(day + dt.timedelta(days=1)).timestamp()
            attrs[f"vrm_{name}_kwh_incl_ev"] = round(sum([v for ts, v in recs.items() if lo <= ts < hi]) / 1000, 2)
    state.set("sensor.ess_load_forecast", round(remaining, 2), attrs)
    return hours, profile, today_h


def _evaluate(hours, profile, today_h=None):
    """Score yesterday against the snapshot taken at its start; snapshot today. Backtest load on first run."""
    hist = _history()
    days = list(hist.get("days", []))
    snap = dict(hist.get("snapshot", {}))
    today = dt_util.now().date()
    yesterday = today - dt.timedelta(days=1)
    done = {d["date"] for d in days}

    def load_actual(day):
        lo, hi = _midnight(day).timestamp(), _midnight(day + dt.timedelta(days=1)).timestamp()
        vals = {int(ts): w for ts, w in hours.items() if lo <= ts < hi}
        if len(vals) < 22:
            return None, None
        hourly = [None] * 24
        for ts, w in vals.items():
            hourly[dt.datetime.fromtimestamp(ts, _tz()).hour] = w
        return sum(vals.values()) / 1000, hourly

    def mae(fc_hourly, act_hourly):
        pairs = [(f, a) for f, a in zip(fc_hourly, act_hourly) if f is not None and a is not None]
        return round(sum([abs(f - a) for f, a in pairs]) / len(pairs)) if pairs else None

    if not days:  # first run: backtest the load model on the last 7 complete days
        for back in range(7, 0, -1):
            day = today - dt.timedelta(days=back)
            act, act_h = load_actual(day)
            if act is None:
                continue
            prof, _ = _profile(hours, day)
            fc_h = prof[_is_workday(day)]
            days.append({"date": day.isoformat(), "backtest": True, "pv_fc_raw": None, "pv_fc": None, "pv_act": None,
                         "load_fc": round(sum(fc_h) / 1000, 1), "load_act": round(act, 1), "load_mae_w": mae(fc_h, act_h)})
        done = {d["date"] for d in days}

    key = yesterday.isoformat()
    if key in snap and key not in done:
        s = snap[key]
        lo, hi = _midnight(yesterday), _midnight(today)
        rows = _statistics([PV_ENERGY], lo, hi, {"change"})
        pv_act = sum([(r.get("change") or 0.0) for r in rows.get(PV_ENERGY, [])])
        act, act_h = load_actual(yesterday)
        days.append({"date": key, "backtest": False, "pv_fc_raw": s.get("pv_fc_raw"), "pv_fc": s.get("pv_fc"),
                     "clearness": s.get("clearness"), "source": s.get("source"), "snow": key in _snow_days(),
                     "pv_act": round(pv_act, 1), "load_fc": s.get("load_fc"),
                     "load_act": round(act, 1) if act is not None else None,
                     "load_mae_w": mae(s.get("load_hourly_w") or [], act_h or [])})

    tkey = today.isoformat()
    if tkey not in snap and dt_util.now().hour < 3:  # a snapshot is only a forecast if taken at the start of the day
        # pv_fc = the forecast in use; pv_fc_raw and clearness always describe VRM (its sunny-day bias learns from them).
        pv = _attrs("sensor.ess_pv_forecast")
        vrm = _attrs("sensor.ess_pv_forecast_vrm")
        cls = _is_workday(today)
        snap = {k: v for k, v in snap.items() if k >= yesterday.isoformat()}
        snap[tkey] = {"pv_fc_raw": vrm.get("raw_today_kwh"), "pv_fc": pv.get("today_kwh"),
                      "clearness": vrm.get("today_clearness"), "source": pv.get("source"),
                      "load_fc": round(sum(today_h or profile[cls]) / 1000, 1),
                      "load_hourly_w": [round(w) for w in (today_h or profile[cls])]}

    days = days[-60:]
    state.set(HISTORY, str(len(days)), {"days": days, "snapshot": snap})

    bias, bias_days = _pv_bias(days, _attrs("sensor.ess_pv_forecast_vrm").get("clear_ref_kwh") or 1.0)
    recent = [d for d in days if d.get("load_act") is not None and d.get("load_fc")][-7:]
    load_ratio = round(sum([d["load_act"] for d in recent]) / sum([d["load_fc"] for d in recent]), 3) if recent else None
    maes = [d["load_mae_w"] for d in recent if d.get("load_mae_w") is not None]
    state.set("sensor.ess_forecast_accuracy", round(bias, 3), {
        "friendly_name": "ESS forecast accuracy", "icon": "mdi:bullseye-arrow",
        "pv_bias": round(bias, 3), "pv_days": bias_days, "pv_source": _attrs("sensor.ess_pv_forecast").get("source"),
        "load_ratio_7d": load_ratio,
        "load_mae_7d_w": round(sum(maes) / len(maes)) if maes else None, "days": days[-14:],
    })


def _publish_ev_learning():
    sessions = list((state.getattr(EV_LEARN) or {}).get("sessions", EV_SEED))[-12:]
    recent = sessions[-8:]
    kwh_pct = pystats.median([s["kwh_per_pct"] for s in recent]) if recent else None
    power = pystats.median([s["power_w"] for s in recent if s.get("power_w")]) if recent else None
    state.set(EV_LEARN, str(len(sessions)), {"sessions": sessions})
    state.set("sensor.ess_ev_learning", round(kwh_pct, 3) if kwh_pct else None, {
        "friendly_name": "ESS EV learned kWh per %", "icon": "mdi:car-battery", "unit_of_measurement": "kWh/%",
        "power_w": round(power) if power else None, "sessions": len(sessions), "last": sessions[-1] if sessions else None,
    })


@state_trigger(EV_STATUS)
def ess_ev_session(value=None, old_value=None):
    """Learn from each home charging session: EVCS energy vs EV SoC gain."""
    if value in ("unavailable", "unknown") or old_value in ("unavailable", "unknown"):
        return  # the EVCS Modbus slave drops out briefly; ignore those flaps
    sess = C["session"]
    guest = state.get("input_boolean.ess_ev_guest_mode") == "on"
    if guest or (sess is not None and sess.get("guest")):
        C["session"] = None  # a guest car says nothing about the car: never learn from it
        return
    if value == "CHARGING" and sess is None:
        C["session"] = {"start": dt_util.now(), "soc_from": _num(CAR_SOC), "soc_max": _num(CAR_SOC),
                        "energy0": _num(EV_ENERGY, 0.0), "power": []}
    elif value != "CHARGING" and sess is not None:
        C["session"] = None
        energy = _num(EV_ENERGY, 0.0)
        kwh = energy - sess["energy0"] if energy >= sess["energy0"] else energy
        soc_from, soc_to = sess["soc_from"], max(sess["soc_max"] or 0, _num(CAR_SOC, 0))
        if soc_from is None or soc_to - soc_from < 5 or kwh < 1:
            log.info(f"ess_forecast: EV session too small to learn from ({kwh:.2f} kWh, {soc_from}->{soc_to} %)")
            return
        power = pystats.median(sess["power"]) if sess["power"] else None
        rec = {"start": sess["start"].isoformat()[:16], "kwh": round(kwh, 2), "soc_from": soc_from, "soc_to": soc_to,
               "kwh_per_pct": round(kwh / (soc_to - soc_from), 3), "power_w": round(power) if power else None}
        sessions = list((state.getattr(EV_LEARN) or {}).get("sessions", EV_SEED)) + [rec]
        state.set(EV_LEARN, str(len(sessions)), {"sessions": sessions})
        log.info(f"ess_forecast: learned EV session {rec}")
        _publish_ev_learning()


@time_trigger("period(now, 1min)")
def ess_ev_session_sample():
    sess = C["session"]
    if sess is None:
        return
    soc = _num(CAR_SOC)
    if soc is not None:
        sess["soc_max"] = max(sess["soc_max"] or 0, soc)
    power = _num(EV_POWER)
    if power and power > 1000:  # skip the taper at the end of a charge
        sess["power"].append(power)


@time_trigger("startup", "cron(12 * * * *)")
def ess_forecast_update():
    task.unique("ess_forecast_update")
    _update_pv()
    hours, profile, today_h = _update_load()
    _evaluate(hours, profile, today_h)
    _publish_ev_learning()


@time_trigger("cron(0,15,30,45 * * * *)")
@state_trigger(SNOW_ON, SNOW_TEMP)
def ess_pv_refresh():
    """PV forecast only, every quarter just before the optimizer (:01/:16/:31/:46), so the snow factor follows the
    day's production; also when the snow helpers change."""
    task.unique("ess_pv_refresh")
    _update_pv()


@state_trigger("input_select.ess_pv_source",
               "input_number.ess_solar_forecast_factor", "input_number.ess_load_forecast_factor",
               "input_number.ess_load_temp_coeff", "input_number.ess_load_temp_balance",
               "input_number.ess_pv_sunny_threshold")
def ess_forecast_factor_changed():
    ess_forecast_update()
