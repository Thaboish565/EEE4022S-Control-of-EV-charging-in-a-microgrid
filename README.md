# Coordinated EV Charging in a Renewable Microgrid Using MPC

Final-year project (EEE4022, University of Cape Town) by Thabo Radjadji.

The project coordinates electric vehicle (EV) charging in a PV-supplied distribution network with a model predictive controller (MPC). The controller keeps the transformer below its rating and the load-bus voltage above 0.95 p.u., charges the most urgent vehicles first, and moves charging into the hours of PV generation. Both charging strategies are run in a DIgSILENT PowerFactory network model, and a SCADA-style dashboard displays the results.

## Repository structure

```
.
├── python_scripts/
│   ├── <uncontrolled script>.py   # uncontrolled baseline: assigns EV parameters, charges at full power
│   └── <controller script>.py     # MPC controller: plans the charging, runs and checks the simulation
├── scada/                          # monitoring dashboard for the exported results
└── README.md
```

Replace the names in angle brackets with the actual file names.

## How it works

1. **Uncontrolled baseline.** Every EV group charges at its maximum power from arrival until it reaches its target state of charge (SOC). This shows what happens to the network without coordination.
2. **MPC controller.** At each hourly step, the controller plans the charging of every group over the rest of the day. It uses the residential-load and PV forecasts and respects the transformer limit (98% of rating), the voltage limit (0.95 p.u.) and each group's departure time. Only the first hour of the plan is applied before re-planning.
3. **Closed-loop check.** After each PowerFactory run, the controller compares the simulated transformer loading with its own estimate, tightens its limit in the hours that went over, and re-plans. This repeats for up to six rounds.
4. **Export and monitoring.** The results of each run are written to CSV files, which the SCADA dashboard displays.

The EVs are modelled as 8 groups of 10 vehicles. Each group represents ten 60 kWh / 15 kW cars, so 600 kWh and 150 kW per group, and the network uses a 0.8 MVA transformer.

## Requirements

- **DIgSILENT PowerFactory** with Python scripting enabled.
- **Python 3**, the version supported by your PowerFactory installation. The scripts use only the standard library and the `powerfactory` module.
- **A PowerFactory project** containing:
  - EV loads named `EV_1` … `EV_8` (class `ElmLod`)
  - a load named `Residential Load`
  - a PV system named `PV System` (class `ElmPvsys`)
  - a two-winding transformer (class `ElmTr2`)
  - a Quasi-Dynamic Simulation command in the study case, recording the transformer `c:loading` and the variables listed in `EXPORT_SIGNALS`
- **Input profiles** in your home folder:
  - `residential_profile_digsilent.csv`, with columns `hour` and `p_grid_kw`
  - `Solar_Temporary.csv`, with columns `Hour` and `PV`

## Running the simulations

1. In PowerFactory, activate the project and the **uncontrolled** study case, then run the uncontrolled script. It writes `results_uncontrolled.csv` and `ev_schedule_uncontrolled_MW.csv` to your home folder.
2. Activate the **controlled** study case and run the controller script. It plans the schedule, attaches it to the EV loads as time characteristics, runs the quasi-dynamic simulation, and corrects the loading limit if needed. It writes:
   - `results_mpc.csv`: transformer loading, voltage, losses, residential load and PV for each hour
   - `ev_schedule_mpc_MW.csv`: the charging power of each group
   - `ev_summary_mpc.csv`: arrival, departure, initial, target and departure SOC of each group
3. Check the end of the controller's output window. The "Departure SOC vs target" lines show whether each group reached its target.

Both scripts must use the same EV parameters (arrival and departure times, SOC, battery capacity and charger power), or the comparison is not valid.

## Configuration

The settings are at the top of each script. The main ones are:

| Setting | Meaning |
|---|---|
| `ARRIVAL_TIMES`, `DEPARTURE_TIMES` | Plug-in window of each EV group (hour of day) |
| `SOC0_LIST`, `TARGET_SOC_LIST` | Initial and target SOC of each group |
| `CAPACITY_KWH_LIST`, `MAX_POWER_KW_LIST` | Battery capacity and maximum charging power per group |
| `TRAFO_KVA` | Transformer rating; `None` reads it from PowerFactory |
| `LOADING_MAX`, `SOFT_LOADING` | Hard loading limit (0.98) and the level where the soft penalty starts (0.95) |
| `V_MIN`, `KAPPA` | Minimum voltage and the voltage sensitivity used by the controller |
| `FILL_TO_MAX` | `False` stops each group at its target SOC |
| `MAX_ITER` | Maximum number of plan-simulate-correct rounds |
| `EXPORT_TAG` | Suffix of the results file, e.g. `mpc` → `results_mpc.csv` |

To keep the results of several test cases, give each run its own `EXPORT_TAG` (for example `mpc_case1`), so later runs don't overwrite earlier ones.

## SCADA dashboard

The `scada/` folder contains the monitoring dashboard. It reads the exported CSV files and shows, for the controlled and uncontrolled runs:

- transformer loading, load-bus voltage and grid losses
- residential load and PV generation
- the EV groups, their arrival and departure times, SOC and charging priority

<!-- Add how to start the dashboard, e.g. which file to open or which command to run, and where it expects the CSV files. -->

The dashboard displays exported results after each simulation; it is not connected to PowerFactory in real time.

## Test cases

| Case | Description |
|---|---|
| 1 | Normal staggered charging (arrivals 07:00–10:00) |
| 2 | Coincident arrival at the evening peak (16:00–23:00) |
| 3 | Early arrival before PV generation (04:00–17:00) |
| 4 | Capacity-limited priority test (all arrive at 09:00, leave between 12:00 and 14:00) |

## Limitations

- All results come from simulation; no hardware-in-the-loop testing was done.
- The load and PV forecasts are assumed to be perfect.
- The EVs are modelled as groups that share arrival time, departure time and initial SOC.
- The controller is a greedy heuristic, not an exact optimiser.

## Author

Thabo Radjadji, University of Cape Town. 
