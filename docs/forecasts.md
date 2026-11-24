# Forecasts (`ess_forecast.py`)

Runs hourly at :12 and at startup; the PV forecast alone is also refreshed every quarter. Every forecast sensor has `today` / `tomorrow` / `day_after` attributes: kWh per quarter from local midnight.

## PV: `sensor.ess_pv_forecast`

Source chosen by `input_select.ess_pv_source`, multiplied by `ess_solar_forecast_factor`.

- **Open-Meteo**: Open-Meteo Solar Forecast entries (one per array), attribute `wh_period_15m` of the `…_energy_production_today` / `_tomorrow` / `_d2` sensors, summed per quarter. No bias correction needed on the author's system (+0.1…+0.3 kWh/day). If a day is incomplete, VRM is used and `source` says so.
- **VRM** (always also published as `sensor.ess_pv_forecast_vrm`): hourly forecast read from the core Victron VRM integration's coordinator, interpolated to quarters while keeping each hour's energy. VRM was 25–55 % low on clear days and 40–55 % high on cloudy days, so a correction is applied **on sunny days only**:
  - clearness = the day's raw forecast / the highest raw daily forecast of the last 14 days;
  - bias = actual/raw from the last 7 sunny days (≥ 3 needed), clamped 0.5–1.8;
  - weight 0 → 1 as clearness goes from `ess_pv_sunny_threshold` − 20 % to the threshold.

Over 23 autumn days, Open-Meteo's day-ahead MAE was 1.91 kWh/day vs 2.67 for bias-corrected VRM.

### Snow compensation

A thin snow layer on the panels cut production to 15–25 % of the forecast for three days on the author's system, while the forecasts saw clear sky. With `input_boolean.ess_pv_snow_compensation` on and the outdoor temperature below `ess_pv_snow_temp` (default 3 °C), the forecast in use is scaled down:

- **Today's factor** = measured production / forecast over the last 2 h of complete quarters (5-minute statistics of the PV energy sensor), extended back until the window holds 0.3 kWh of forecast. It is clamped to 0–1, so it never raises the forecast, and a ratio of 0.85 or more counts as 1.0 (ordinary forecast error, not snow). An earlier version weighted all of the day's quarters by age; the bright morning then dominated and the factor recovered too slowly after the snow slid off.
- It needs ≥ 0.5 kWh of forecast in today's elapsed quarters. Before that (night, early morning), the last measured factor (the evening's window value) is carried for up to 36 h.
- **Tomorrow and the day after** get the same factor when their forecast mean temperature is also below the threshold.
- At or above the threshold, nothing is scaled and the carried factor is cleared.

The factors are in the attributes `today_snow_factor` / `tomorrow_snow_factor` / `day_after_snow_factor`, and `snow` holds the reason. `*_raw_kwh` and `sensor.ess_pv_forecast_vrm` stay uncompensated. `pyscript.ess_pv_snow` keeps the carried factor and each day's actual / forecast (`day_ratio`). Days below 0.85 count as snow days and are skipped by the VRM sunny-day bias learning. The PV forecast is refreshed every quarter (:00/:15/:30/:45, just before the optimizer), so the factor follows the day.

Solcast-style forecasts that need DC power per panel group do not fit AC-coupled systems.

## Load: `sensor.ess_load_forecast`

House load **excluding EV charging** (consumption L1–L3 minus EVCS power), from HA hourly statistics.

1. **Temperature model**: heating W = `ess_load_temp_coeff` × (`ess_load_temp_balance` − outdoor temperature), per hour (author: 70 W/°C below 9 °C, fitted on 2.5 years of data). Coefficient 0 = off.
2. History is normalised (heating removed), then two hour-of-day profiles are built: **workdays** of the last 14 days (half-life 4 days) and **free days** of the last 28 days (half-life 7 days).
3. Forecast = profile + heating at the forecast temperature (hourly for 48 h, then daily mean) × `ess_load_forecast_factor`. `SENSOR_VS_FORECAST` corrects a constant offset between your outdoor sensor and the weather service.
4. EV charging that the EVCS did not report (EVCS offline) is detected as an L1 hour above `L1_EV` and replaced by the typical L1 load. Adapt the phase if your car charges on another one.

Day-ahead daily RMSE on the author's data: 5.35 kWh without temperature → 3.85 with temperature and workday split.

With the constant efficiency model, the optimizer adds `ess_standby_loss` (the Multi's own consumption, not in the consumption sensors). The measured model contains it.

## Accuracy: `sensor.ess_forecast_accuracy`

At 00:12, yesterday is scored against the snapshot taken at its start. State = PV bias; attributes hold load actual/forecast over 7 days (`load_ratio_7d`, target ≈ 1.0), hourly MAE and a daily table. Use it to tune the two factors.

## EV learning: `sensor.ess_ev_learning`

Each home session (≥ 5 % SoC, ≥ 1 kWh, not guest) gives grid kWh per % SoC and median charge power. The median of the last 8 sessions replaces `ess_ev_kwh_per_percent` / `ess_ev_charge_power` when `ess_ev_use_learned` is on and ≥ 3 sessions exist. `EV_SEED` holds two starting sessions; replace them with your car's.
