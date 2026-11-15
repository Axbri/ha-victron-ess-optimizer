# EV charging

Built for a **Victron EVCS** controlled through the GX (Modbus TCP, EVCS in Manual mode). Other chargers need the EVCS entities in `ess_executor.py` replaced by your charger's switch, current and status entities, and the status strings (`DISCONNECTED`, `CHARGING`, `CHARGED`) mapped.

Status in `sensor.ess_ev` (target, needed kWh, latest start, next planned charge, preheat, departure source).

## Modes

`input_select.ess_ev_mode` (Auto / Charge now / Off) and `input_boolean.ess_ev_smart_control`:

| State | Behaviour |
| --- | --- |
| Smart control **off** (passive) | Charger kept on at max current; the car's own timer decides; not in the plan |
| Smart control on, Auto | Charger on only in quarters with `ev_kw > 0` in a fresh plan, during the deadline guard or preheat; otherwise off. **The car's own timer must be off** |
| Charge now | Charges until full or unplugged, then back to Auto |
| Off | Charger off; notifies if a departure target will be missed |

## Target and departure

- Departure: `ess_ev_departure` on workdays (Workday integration); `ess_ev_next_departure` Yes/No overrides the next one once.
- **Manual departure** (`ess_ev_manual_departure` + `ess_ev_manual_departure_time` + `ess_ev_manual_target`), e.g. a weekend trip: used when it comes before the next workday departure; switches itself off after use.
- **Target** linear in the forecast temperature at departure: `ess_ev_cold_temp` → `ess_ev_target_winter`, `ess_ev_warm_temp` → `ess_ev_target_summer`, clamped outside. `ess_ev_season` Summer/Winter forces one. Capped at `ess_ev_max_reachable_soc` (many cars stop charging below 100 % as displayed).
- **Taper**: above `EV_TAPER_FROM` (92 %) each percent takes `EV_TAPER_MIN_PER_PCT` (8 min) longer; this time is added to the needed energy so the plan reserves it. Fit both to your car.
- **Reached latch**: once the target is reached (or the charger reports `CHARGED` within 5 % of it), small SoC drops (e.g. from preheat) do not restart charging.

## Deadline guard

Latest start = departure − `ess_ev_finish_margin` − needed energy / charge power. From then on it charges regardless of price. If the car does not start, `button.ev_charge_start` is pressed (2 attempts), then a notification is sent.

## Preheat (optional, `ess_ev_preheat`)

Starts the car's climate before each departure, lead time 10 min at 20 °C → 30 min at −10 °C (extrapolated below; none above 20 °C), using the car's outdoor sensor if fresh, otherwise the forecast. The charger is kept on from then until departure so the heat comes from the house, not the car's battery. Retries twice; stops the climate if the car is still plugged in 30 min after departure. The automation `EV climate off after workday departure` stops a climate that would otherwise restart after parking (car-specific; skip if not needed).

## EV and the house battery

`ess_allow_battery_to_ev` on: the house battery may supply the EV (everything is behind the Multi). Off: the setpoint is raised by the EVCS power, so the grid supplies the car.

## Guest charging

For charging other cars without touching the house battery.

- `ess_ev_guest_mode` → charger on at once at `ess_ev_guest_current`, no SoC logic. The setpoint is raised by the EVCS power in every mode, so the battery does not feed the guest car (except above `ess_max_import`, where the clamp lets the battery cover the excess). PV surplus the battery cannot take flows into the car.
- **Counter** `pyscript.ess_guest_charging` (`ess_guest.py`, survives restarts): every 10 s energy, grid share = min(EVCS power, grid import), solar share = rest, cost = grid share × import price. A session ends when guest mode turns off or the car is unplugged; guest mode then turns off and a summary is sent.
- **Automatic guest mode** (automations): on when the owner leaves home (and re-armed after a session while away), never while the owner's EV is plugged in; off when the owner is home, the owner's EV is plugged in and the EVCS sees a car (all three, since phone presence can lag the plug-in by minutes).

## Field notes

- The EVCS charges on its own when it loses the GX connection; a weak Wi-Fi link makes smart control unreliable. A notification fires after 15 min offline while plugged in.
- Some cars latch a 100 % SoC after a stopped charge; check that your SoC entity reports the real value after a planned stop.
