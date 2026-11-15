# Configuration

## Entities to map

The scripts read and write the entities below. Replace them with yours using search and replace across `ha/` (some names also occur outside the constant blocks, e.g. in trigger lists and the dashboard). Victron entity names follow the integration's pattern `<device>_<register>_<unit ID>`; the author's unit IDs are **223** (battery monitor, SmartShunt), **224** (MPPT), **227** (VE.Bus), **40** (EVCS).

| Entity in the repo | What it must be | Used by |
| --- | --- | --- |
| `number.victron_settings_ess_acpowersetpoint` | ESS grid setpoint (register 2700) | executor, watchdog |
| `number.victron_settings_ess_batterylife_minimumsoc` | ESS minimum SoC (2901) | executor, watchdog |
| `number.victron_settings_ess_maxchargecurrent` | DVCC max charge current (2705, −1 = no limit) | executor, watchdog |
| `switch.victron_settings_ess_preventfeedback` | Block AC-coupled PV feed-in | executor |
| `switch.victron_settings_ess_overvoltagefeedin` | Feed in DC (MPPT) PV surplus | executor |
| `sensor.victron_battery_soc_223` | Battery SoC | optimizer, executor |
| `sensor.victron_battery_history_timesincelastfullcharge_223` | Time since last full charge (s) | optimizer (SoC sync) |
| `sensor.victron_system_consumption_l1..l3` | AC consumption per phase (incl. EVCS) | forecast |
| `sensor.victron_total_ac_out_consumption` | Sum of the above (template) | executor (HOLD) |
| `sensor.pv_power` | Total PV power, AC + DC (template) | executor (HOLD) |
| `sensor.pv_energy_total` / `sensor.pv_energy_today` | PV energy counter / daily meter (`ess_pv_energy.yaml`) | forecast, optimizer (selling gate) |
| `sensor.om_solar_<array>_energy_production_` | Open-Meteo Solar Forecast prefix per array (`OM_PREFIX`) | forecast |
| `switch.victron_evcharger_startstop_40` | EVCS start/stop (3826) | executor |
| `number.victron_evcharger_setcurrent_40` / `_maxcurrent_40` | EVCS set current (3825) / max current | executor |
| `sensor.victronevcharger_total_power40` | EVCS power | all scripts |
| `sensor.victronevcharger_status40` | EVCS status (`DISCONNECTED`, `CHARGING`, `CHARGED`, …) | all scripts |
| `sensor.victronevcharger_energy_forward40` | EVCS session energy | forecast (EV learning) |
| `sensor.grid_power` | Grid power at the connection point, W, + = import (template) | guest counter |
| `sensor.outdoor_temperature` | Outdoor temperature (load model, EV target fallback) | forecast, executor |
| `weather.forecast_home` | Weather with hourly forecast | forecast, executor |
| `binary_sensor.workday_sensor` | Workday integration | forecast, executor |
| `sensor.ev_soc` | EV state of charge, % | executor, forecast |
| `binary_sensor.ev_plugged_in` | EV plugged in | executor, automations |
| `button.ev_charge_start` | Wakes the car / starts charging (optional) | executor |
| `binary_sensor.ev_climate_active`, `button.ev_climate_start`, `button.ev_climate_stop`, `sensor.ev_outdoor_temperature` | Cabin preheat (optional) | executor, automation |
| `person.owner` | The EV's driver | automations |
| `notify.mobile_app_your_phone` (`NOTIFY` in scripts) | Notification target | executor, guest, watchdog |

**Without car SoC** (no integration for the car): `ess_ev_smart_control` must stay off. The charger is then kept on and the car's own timer decides; guest mode still works. With smart control on and no SoC, the executor assumes 0 % and charges at once (deadline guard).

**Without the optional car buttons**: turn `ess_ev_preheat` off and skip the automation `EV climate off after workday departure`; a press on a missing button only logs a warning.

**Without a grid meter**: with all loads on the Multi's AC-out (author's setup), AC-in power = grid; make `sensor.grid_power` a template of the VE.Bus AC-in power.

**Different topology** (loads on AC-in, Victron grid meter): the setpoint semantics are the same (ESS setpoint at the grid meter). `victron_total_ac_out_consumption` must then cover all house consumption, and the load forecast's consumption sensors must include loads on AC-in.

**Constants in the scripts** worth checking: `EV_TAPER_FROM`, `EV_TAPER_MIN_PER_PCT` (the car's charge taper, executor), `EV_START_COST` (optimizer), `SENSOR_VS_FORECAST`, `L1_EV`, `EV_SEED` (forecast), and the measured efficiency curves in `ess_dp.Params` ([optimizer.md](optimizer.md#efficiency)). Currency strings assume SEK.

## Helpers

`ha/packages/ess_helpers.yaml` defines all helpers without initial values. The author's values (Nov 2026; 28 kWh LiFePO4, 9.2 kWp PV, 16 A 3-phase fuse, Nissan Leaf 30 kWh) as a starting point:

| Helper | Name | Author's value |
| --- | --- | --- |
| `ess_battery_capacity` | ESS battery capacity | 29 kWh |
| `ess_energy_tax` | ESS energy tax | 36 öre/kWh |
| `ess_ev_charge_power` | ESS EV charge power | 3600 W |
| `ess_ev_cold_temp` | ESS EV cold temp | 0 °C |
| `ess_ev_finish_margin` | ESS EV finish margin | 30 min |
| `ess_ev_guest_current` | ESS EV guest current | 16 A |
| `ess_ev_kwh_per_percent` | ESS EV kWh per percent | 0.27 kWh/% |
| `ess_ev_manual_target` | ESS EV manual target | 80 % |
| `ess_ev_max_reachable_soc` | ESS EV max reachable SoC | 95 % |
| `ess_ev_opportunistic_max_price` | ESS EV opportunistic max price | 180 öre/kWh |
| `ess_ev_opportunistic_target` | ESS EV opportunistic target | 80 % |
| `ess_ev_target_summer` | ESS EV target at warm temp | 70 % |
| `ess_ev_target_winter` | ESS EV target at cold temp | 100 % |
| `ess_ev_warm_temp` | ESS EV warm temp | 20 °C |
| `ess_export_bonus` | ESS export bonus | 10 öre/kWh |
| `ess_export_net_benefit` | ESS export net benefit | 5.86 öre/kWh |
| `ess_full_charge_interval` | ESS full charge interval | 14 d |
| `ess_grid_fee` | ESS grid fee | 73.4 öre/kWh |
| `ess_grid_offset` | ESS grid offset | 0 W |
| `ess_load_temp_balance` | ESS load balance temperature | 9 °C |
| `ess_load_forecast_factor` | ESS load forecast factor | 95 % |
| `ess_load_temp_coeff` | ESS load temperature coefficient | 70 W/°C |
| `ess_main_fuse` | ESS main fuse | 16 A |
| `ess_manual_export_floor` | ESS manual export floor | 40 % |
| `ess_manual_power` | ESS manual power | 4400 W |
| `ess_max_export` | ESS max export | 10000 W |
| `ess_max_grid_charge` | ESS max grid charge | 9000 W |
| `ess_max_import` | ESS max import | 10000 W |
| `ess_max_soc_grid_charge` | ESS max SoC grid charge | 100 % |
| `ess_min_soc` | ESS min SoC | 8 % |
| `ess_night_reserve_margin` | ESS night reserve margin | 20 % |
| `ess_override_hours` | ESS override hours | 4 h |
| `ess_peak_hour_fee` | ESS peak hour fee | 0 öre/kWh |
| `ess_pv_sunny_threshold` | ESS PV sunny threshold | 60 % |
| `ess_reserve_solar_factor` | ESS reserve solar factor | 70 % |
| `ess_round_trip_efficiency` | ESS round trip efficiency | 80 % |
| `ess_sell_min_pv_today` | ESS sell min PV today | 19 kWh |
| `ess_sell_spike_price` | ESS sell spike price | 4 SEK/kWh |
| `ess_solar_forecast_factor` | ESS solar forecast factor | 100 % |
| `ess_standby_loss` | ESS standby loss | 40 W |
| `ess_supplier_markup` | ESS supplier markup | 6 öre/kWh |
| `ess_supplier_variable` | ESS supplier variable | 5.32 öre/kWh |
| `ess_vat` | ESS VAT | 25 % |
| `ess_wear_cost` | ESS wear cost | 5 öre/kWh |
| `ess_allow_battery_export` | ESS allow battery export | off |
| `ess_allow_battery_to_ev` | ESS allow battery to EV | on |
| `ess_allow_grid_charge` | ESS allow grid charge | on |
| `ess_allow_solar_export` | ESS allow solar export | on |
| `ess_block_export_negative_price` | ESS block export negative price | on |
| `ess_ev_guest_mode` | ESS EV guest mode | off |
| `ess_ev_manual_departure` | ESS EV manual departure | off |
| `ess_ev_preheat` | ESS EV preheat | on |
| `ess_ev_smart_control` | ESS EV smart control | on |
| `ess_ev_use_learned` | ESS EV use learned | on |
| `ess_force_full_charge` | ESS force full charge | off |
| `ess_measured_efficiency` | ESS measured efficiency | on |
| `ess_ev_mode` | ESS EV mode | Auto |
| `ess_ev_next_departure` | ESS EV next departure | Auto |
| `ess_ev_season` | ESS EV season | Auto |
| `ess_mode` | ESS mode | Auto |
| `ess_pv_source` | ESS PV source | Open-Meteo |
| `ess_ev_departure` | ESS EV departure | 06:20:00 |
