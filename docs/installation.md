# Installation

Plan for an evening of setup and a few days of supervised running. Every step keeps the system in a safe state: until you select `Auto`, nothing is written to the inverter except by your own choice.

## 1. Victron side

| Setting (GX → Settings) | Value |
| --- | --- |
| Services → Modbus TCP | Enabled |
| ESS → Mode | Optimized (with or without BatteryLife; author: without) |
| ESS → Grid metering | Your real setup. Author: no grid meter, all loads on AC-out, so the Multi's AC-in = grid ("inverter/charger" metering) |
| ESS → Minimum SoC | Your floor (author: 8 %); the executor writes this register, `ess_min_soc` is the source of truth |
| Dynamic ESS | **Off**. It writes the same setpoint |
| EVCS (if used) | Mode **Manual**, GX connection over Modbus TCP, stable Wi-Fi/Ethernet |
| Multi AC-in current limit | Your main fuse. This is the hardware protection; the software limit comes on top |

Note your Modbus unit IDs (GX → Settings → Services → Modbus TCP → Available services): battery monitor, VE.Bus, MPPTs, EVCS. The Victron integration puts them in the entity IDs, e.g. `sensor.victron_battery_soc_223`.

## 2. Home Assistant integrations

1. **Victron GX Modbus TCP** (HACS, `sfstar/hass-victron`). Enable write support in the options. Apply [`patches/hass-victron-v0.6.4.patch`](../patches/hass-victron-v0.6.4.patch) (see [operations.md](operations.md#victron-integration-patches)), then restart HA.
2. **Pyscript** (HACS). In its options enable **Allow all imports** and **Access hass as a global variable** (the optimizer imports `ess_dp`; the forecast reads the VRM coordinator).
3. **Nord Pool** (HACS) with 15-minute prices in your currency including VAT.
4. **PV forecast**: Open-Meteo Solar Forecast (HACS) with one entry per array (name them so the entities start with `sensor.om_solar_<name>_energy_production_`) and/or the core **Victron VRM** integration. Set `OM_PREFIX` in `ess_forecast.py`.
5. **Workday** (core) for your country; **weather** entity with hourly forecasts (met.no default `weather.forecast_home`).
6. Optional: an EV integration that exposes the car's SoC; a P1/HAN grid meter.
7. Dashboard: **apexcharts-card** (HACS frontend).

## 3. Copy the files

| Repo | HA `/config` |
| --- | --- |
| `ha/ess_lib/ess_dp.py` | `ess_lib/ess_dp.py` |
| `ha/pyscript/*.py` | `pyscript/` |
| `ha/packages/*.yaml` | `packages/` (add `homeassistant: packages: !include_dir_named packages` to `configuration.yaml`) |
| `ha/templates_ess.yaml` | merge into your template config (`template: !include ...`) |
| `ha/automations_ess.yaml` | append to `automations.yaml` |

Before copying the Pyscript files, edit the entity constants at the top of each file ([configuration.md](configuration.md)). Copy `ess_dp.py` and the packages first, the Pyscript files last.

## 4. Configure

1. Restart HA (packages and templates need it). Check **Settings → System → Logs** for Pyscript errors.
2. Set every helper to a sensible value ([configuration.md](configuration.md#helpers)), especially: fees, `ess_battery_capacity`, `ess_min_soc`, `ess_max_import` (≈ main fuse × 230 V × 3 minus margin), `ess_max_export`, `ess_max_grid_charge`.
3. **Keep `ess_mode` = `Victron default`, `ess_allow_battery_export` off, `ess_ev_smart_control` off.**
4. Check that `sensor.ess_import_price` / `sensor.ess_export_price` match your invoice, and that `sensor.ess_pv_forecast` and `sensor.ess_load_forecast` have `today` / `tomorrow` arrays.

## 5. Commission

| Step | Do | Check |
| --- | --- | --- |
| 1 | Leave `Victron default` for a day | `sensor.ess_plan` updates every 15 min; the plan looks sane on the Plan view |
| 2 | Select `Self-consumption` | Setpoint = `ess_grid_offset`, min SoC = `ess_min_soc`; the battery behaves like plain ESS |
| 3 | Test `Hold`, `Charge now`, `Export now` briefly | Grid power follows; `sensor.ess_executor` shows the reason and writes per hour |
| 4 | Stop Pyscript (or rename `ess_executor.py`) for 3 min | The watchdog notifies and resets the registers |
| 5 | Select `Auto` | The executor follows the plan; compare plan vs actual SoC for a few days |
| 6 | EV: turn `ess_ev_smart_control` on, the car's own charge timer off | Charging happens in the planned quarters; the deadline guard holds the departure ([ev-charging.md](ev-charging.md)) |
| 7 | Later (spring): `ess_allow_battery_export` on | Selling only under the rules in [optimizer.md](optimizer.md#selling) |

Way back at any time: `ess_mode` = `Victron default`.

## 6. Dashboard

Create a dashboard (Settings → Dashboards → Add), open the raw configuration editor, paste `ha/dashboards/energi-ess.yaml`. Replace the Victron unit numbers and other placeholders as in the scripts.
