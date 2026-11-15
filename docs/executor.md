# Executor (`ess_executor.py`)

A 10-second loop (plus triggers on helper changes) that turns the requested mode into Victron register writes. Status in `sensor.ess_executor` (effective mode, reason, targets, writes per hour).

`input_select.ess_mode`: **Auto** / Self-consumption / Hold / Charge now / Export now / Victron default.

- Auto follows the current quarter of `sensor.ess_plan`; a missing plan or one older than 45 min → self-consumption.
- Manual modes expire after `ess_override_hours`; Charge now and Export now reset to Auto at startup.

| Effective mode | Victron action |
| --- | --- |
| SELF | Setpoint = `ess_grid_offset` (+ EVCS power if the EV is grid-fed). Min SoC = `ess_min_soc` |
| HOLD | Min SoC = floor(SoC) − 1, ratcheting up when PV raises the SoC; setpoint = consumption − PV, so the grid carries the load (deadband 400 W). At min SoC the setpoint is just the offset |
| CHARGE | Setpoint = planned grid import (+ grid-fed EV), capped by `ess_max_import`; HOLD once `ess_max_soc_grid_charge` is reached |
| EXPORT | Setpoint = planned grid power (negative). **Min SoC = export floor**, so the GX stops by itself. Only when selling is allowed |
| SOLAR_EXPORT | DVCC max charge current = 0 (PV goes to the grid, deficits still from the battery); −1 otherwise |
| Victron default | Releases everything once (setpoint 0, min SoC, DVCC −1, feed-in off), then idles |

In all modes except Victron default, AC PV feed-in (PreventFeedback off) and DC PV feed-in (OvervoltageFeedIn on) are enabled when solar export is allowed and the export price is ≥ 0 (or blocking is off). PreventFeedback does not stop battery export.

## Writes

- Only when the value must change (setpoint deadband 100 W; 200 W with a charging EV; 400 W in HOLD), at most once per 5 s per register.
- Every setpoint is clamped to `ess_max_import` / −`ess_max_export` and the register range (±11 000 W on the author's system). Above the import limit the battery covers the rest, which protects the main fuse.
- Integration readback lags one poll, so a write is trusted for 60 s.
- Typically 0–10 writes per hour.

## Robustness

- Each executor instance publishes `instance` / `started`; an older instance stands down when a newer one publishes (Pyscript reload races).
- Errors in the EV logic are caught and never stop battery control.

## Register reference (Victron GX, com.victronenergy.settings / evcharger)

| Register | Meaning |
| --- | --- |
| 2700 | ESS AC power setpoint (W, signed, at the grid meter / AC-in) |
| 2705 | DVCC max charge current (A, −1 = no limit) |
| 2707 / 2708 | Feed-in of DC PV / prevent AC PV feed-in |
| 2901 | ESS minimum SoC (× 10) |
| 3825 / 3826 | EVCS set current / start-stop |
