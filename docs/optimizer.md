# Optimizer (`ess_dp.py` + `ess_optimizer.py`)

`ess_optimizer.py` collects prices, forecasts, SoC, EV demand and helpers, runs `ess_dp` in a worker thread (~1–2 s) and publishes `sensor.ess_plan`. It runs at :01/:16/:31/:46, at startup, on helper changes and on EV plug-in/unplug or a changed departure/target.

## Method

* **Dynamic programming** over battery energy (`ess_battery_capacity`), 0.2 kWh grid with linear interpolation. Each quarter also offers the exact self-consumption move, so small night loads are followed exactly. The forward pass follows the continuous SoC.

* **Horizon**: current quarter → last published price (midnight, or tomorrow midnight after \~13:00). Quarters are keyed by UTC, so the repeated autumn DST hour is kept.

* **Objective**: import cost − export revenue + wear (`ess_wear_cost` per kWh discharged) + terminal value.

* **Limits**: `ess_max_import`, `ess_max_grid_charge`, `ess_max_export`; grid charging only below `ess_max_soc_grid_charge` and if `ess_allow_grid_charge`.

## Efficiency

`ess_measured_efficiency`:

* **On (measured, power-dependent)**: curves fitted on 5-minute night data of the author's 3-phase Multi (~12 kW): inverter AC = 0.9953·DC − 0.0725 kW (≈ 80 % at 0.25 kW, 97 % above 2.5 kW); charger DC = 0.9452·AC − 0.0060·AC² − 0.0581 kW (≈ 90 % at 2–5 kW); battery 96 % round trip; idle 45 W. Each DP step uses the exact power of the move, so the plan prefers moderate charge power (2–4 kW instead of 9 kW bursts) and avoids low-power battery use that gains nothing. Night SoC validation RMSE 2.8 % vs 4.1 % for the constant model. **Refit for your hardware** (`Params` in `ess_dp.py`) or use the constant model.

* **Off (constant)**: `ess_round_trip_efficiency` split evenly + `ess_standby_loss` added to the load.

Where selling is not allowed, a PV surplus must be stored as far as plain self-consumption would store it (no deliberate PV export).

## Terminal value

Energy left at the horizon end is valued at the 10th-percentile import price (it replaces later imports). Only the part that tomorrow's forecast PV surplus would push out of a full battery is valued at the median export price.

## Export floor (night reserve)

min SoC + the pessimistic energy deficit (PV × `ess_reserve_solar_factor`) until the next PV surplus, × (1 + `ess_night_reserve_margin`). It limits selling only, and the executor writes it as the ESS minimum SoC during export, so the GX stops by itself if HA dies.

## Selling

Battery export is allowed only when **all** hold:

1. `ess_allow_battery_export` is on;
2. tomorrow's prices are published (the plan then sees the whole night);
3. today's PV (`sensor.pv_energy_today`) ≥ `ess_sell_min_pv_today` (0 = off).

**Spike exception** (`ess_sell_spike_price`, export price in SEK/kWh, 0 = off): selling is allowed in any quarter at or above the threshold regardless of rules 1–3 (per-quarter `sell_mask`). The reserve for those quarters also counts the cheapest 25 % grid quarters as refill points. Backtest Jan–Oct 2026 vs no selling: 1.75 → +259 SEK, 2.5 → +96, 3.0 → +38; lower thresholds earn mostly in summer (where rules 1–3 apply anyway) and lose in autumn. The author uses 4.0: ordinary evening peaks at 3.3–3.7 gave only break-even.

Outside allowed quarters the battery never exports. PV surplus with a full battery is still exported unless `ess_allow_solar_export` is off, or the export price is negative and `ess_block_export_negative_price` is on.

## SoC sync (full charge)

When the time since the last full charge ≥ `ess_full_charge_interval` days, or `ess_force_full_charge` is on, 100 % is required by the next 06:00 (5 SEK/kWh shortfall penalty). The target is kept by every re-plan until 06:00; if missed and tomorrow's 06:00 is not yet in the horizon, the horizon end becomes the target. Costs \~5–8 SEK in winter. Keeps the shunt's SoC calibrated and the LiFePO4 cells balanced.

## EV in the plan (smart control only)

* The marginal energy cost per quarter selects the cheapest quarters before the deadline (departure − `ess_ev_finish_margin`) for the energy needed. Two rounds.

* Opportunistic charging up to `ess_ev_opportunistic_target` where the marginal cost ≤ `ess_ev_opportunistic_max_price` (e.g. PV surplus).

* Each charge start costs `EV_START_COST` (0.15 SEK) in the selection, so near-equal quarters form 1–2 blocks instead of many short charges.

## `sensor.ess_plan`

State = mode of the current quarter. Attributes per quarter: `modes` (S self-consumption, H hold, C grid charge, E export, X sell PV with battery idle), `soc`, `batt_kw`, `grid_kw`, `ev_kw`, `floor`, `pv_kw`, `load_kw`. Plus `summary`, `cost` / `baseline_cost` / `savings` vs plain self-consumption (negative while a SoC sync is planned), `selling_allowed`, `selling_note`, `full_charge_due`, `eff_model`, `runtime_s`.

`ess_dp.py` has no Home Assistant imports, so it can be run offline for backtests on exported history.
