# Operations and pitfalls

## Victron integration patches

`patches/hass-victron-v0.6.4.patch` fixes three bugs in Victron GX Modbus TCP v0.6.4 (`custom_components/victron/`). Apply with `git apply` or by hand, then restart HA (a config-entry reload does not re-import Python code). A HACS update overwrites the patches; check whether upstream has fixed them before updating.

| File | Problem | Effect without the patch |
| --- | --- | --- |
| `number.py` | A Modbus unit without data at startup (PV inverter asleep, EVCS offline) raises `KeyError`, which aborts the whole number platform | `number.victron_settings_ess_acpowersetpoint` unavailable → no ESS control |
| `number.py` | Negative values encoded as 65535 + v instead of 65536 + v | Writing −1 (DVCC "no limit") writes −2, which the GX rejects |
| `const.py` | `vebus_microgrid_error` gets its entity type passed as `unit` | A Python object as unit breaks entity-registry saves, recorder statistics and `/api/states` |

After any HA or HACS update, check that the entity registry is still saved (`.storage/core.entity_registry` changes) and that 5-minute statistics are still written.

## Pyscript

- **Deploy by copying** the file into `/config/pyscript/`; Pyscript reloads it. Do not also call `pyscript.reload`: the race can leave an orphaned instance that only an HA restart removes (the executor's instance check limits the damage).
- After changing `ess_dp.py`, re-copy (touch) `ess_optimizer.py`: the core is re-imported only when the script reloads.
- No generator expressions (use list comprehensions); Futures from `async_add_executor_job` need an explicit `await`.
- Time triggers (`period`, `cron`) freeze during the repeated autumn DST hour; the keepalive automation covers it.
- Pyscript starts only when HA reaches RUNNING; a slow integration can delay it by minutes.

## Home Assistant

- Create UI helpers **without `initial`**; a helper with `initial` resets at every restart.
- HA skips a state write when attributes compare equal (95 == 95.0).
- apexcharts `data_generator` code must not declare `start`, `end`, `entity`, `hass` or `moment`.
- Edit dashboards through the UI or WebSocket `lovelace/config/save`, never `.storage`.
- The Victron integration can log "No valid data returned for slave …" tens of thousands of times a day for sleeping units; a `logger` filter cleans it up.

## Maintenance

| When | What |
| --- | --- |
| Monthly invoice | Update variable fee helpers, then `template.reload` |
| Weekly at first | `sensor.ess_forecast_accuracy`: PV bias and `load_ratio_7d` ≈ 1; adjust the forecast factors |
| After two weeks of EV nights | Refit the taper constants and `ess_ev_max_reachable_soc` |
| After a winter | Refit `ess_load_temp_coeff` / balance; recalibrate `ess_battery_capacity` (SoC change vs shunt energy) |
| Spring | Turn on `ess_allow_battery_export` if selling pays on your tariff |

## Troubleshooting

| Symptom | Look at |
| --- | --- |
| `sensor.ess_plan` unavailable | Prices, forecasts or SoC missing (attribute/reason in the log); normal for ≤ 15 min after restart |
| Plan `savings` < 0 without a SoC sync | Infeasible states; check forecasts and limits |
| Plan SoC drifts from actual | Capacity or efficiency model; load forecast level |
| Many setpoint writes per hour | Deadbands; an oscillating EV or HOLD consumption |
| EV not charging in planned quarters | `sensor.ess_ev` status; EVCS mode Manual; car timer off; EVCS offline |
