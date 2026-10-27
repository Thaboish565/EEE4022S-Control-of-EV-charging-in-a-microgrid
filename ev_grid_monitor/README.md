# EV Charging & Grid Monitor

Runs your PowerFactory MPC (and optional uncontrolled) script, reads the results, shows the dashboard.

## Setup
1. `pip install -r requirements.txt`
2. Add the block in `mpc_export_snippet.py` to your MPC script (inside `main()`, after the Step 5 CSV block). It writes `ev_summary_mpc.csv` next to your existing `ev_schedule_mpc_MW.csv`.
3. Edit `config.json`: `pf_python_path`, the scenario `project`/`script` paths, `study_case` (leave empty to use the project's active one), and the `signals` object/variable names.
4. Set `"demo_mode": false`, run `python server.py`, open http://localhost:5000, click **Run study**.

Run the server as the same Windows user that runs the script (files are read from your home folder).
For the comparison, set `scenarios.uncontrolled.enabled` to true. The uncontrolled run goes first, so the MPC files are the ones left on disk.
