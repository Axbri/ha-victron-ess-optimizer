# Safety and fallback

| Situation | Behaviour |
| --- | --- |
| Executor stops updating `sensor.ess_executor` for 3 min (mode not Victron default) | **Watchdog automation** sets setpoint 0, min SoC = `ess_min_soc`, DVCC −1 and notifies |
| Pyscript time triggers freeze (repeated autumn DST hour) | **Keepalive automation** (every 20 s) calls `pyscript.ess_executor_keepalive` if the executor is > 30 s stale and `pyscript.ess_optimize` if the plan is > 20 min old |
| Plan missing, older than 45 min, or SoC unavailable | Self-consumption |
| HA restart | Charge now / Export now reset to Auto; `sensor.ess_plan` is `unavailable` until the next quarter-hour run |
| HA dies during EXPORT | ESS min SoC = export floor, so the GX stops selling by itself |
| HA dies during CHARGE | Worst case: battery filled at that quarter's price |
| Grid limit | Setpoints never exceed `ess_max_import`; the Multi's AC-in current limit is the hardware backstop |
| EV logic error | Caught; battery control continues |
| Way back to plain Victron | `ess_mode` = Victron default |

Never run another automation that writes the ESS setpoint (old price automations, Victron Dynamic ESS) alongside the executor.
