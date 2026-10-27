"""EV Charging & Grid Monitor - backend.
Runs your PowerFactory scenario script(s), reads ElmRes results, serves the dashboard.
Run:  python server.py   ->  http://localhost:5000
Run it as the same Windows user that runs the MPC script (it reads/writes files in ~).
"""
import copy, csv, json, math, runpy, sys, threading, time
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify, send_from_directory

BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text())
OUT = Path(CFG["output_dir"] or Path.home())
app = Flask(__name__, static_folder=str(BASE / "static"))
STATE = {"status": "idle", "message": "No results yet", "results": None, "updated": None}
LOCK = threading.Lock()  # PowerFactory is single-threaded: one run at a time


# ---------- PowerFactory ----------
def run_scenario(pfa, sc, label):
    """Activate project (+study case), run the user's script, read the result file."""
    STATE["message"] = f"Running {label} scenario in PowerFactory..."
    if pfa.ActivateProject(sc["project"]):
        raise RuntimeError(f"Project '{sc['project']}' not found")
    if sc.get("study_case"):
        cases = pfa.GetProjectFolder("study").GetContents(sc["study_case"] + ".IntCase")
        if not cases:
            raise RuntimeError(f"Study case '{sc['study_case']}' not found")
        cases[0].Activate()
    runpy.run_path(sc["script"], run_name="__main__")  # your script also runs the QDS

    study = pfa.GetActiveStudyCase()
    res = (study.GetContents((CFG["result_file"] or "*") + ".ElmRes") or [None])[0]
    if res is None:
        raise RuntimeError(f"No ElmRes result file in study case after the {label} run")
    res.Load()
    cols = {}
    for key, s in CFG["signals"].items():
        objs = pfa.GetCalcRelevantObjects(s["object"])
        c = res.FindColumn(objs[0], s["var"]) if objs else -1
        if c >= 0:
            cols[key] = c
        else:
            print(f"[warn] {label}: '{key}' skipped ({s['object']} / {s['var']} not found in results)")
    t0 = datetime.fromisoformat(CFG["start_date"])
    times, series = [], {k: [] for k in cols}
    for r in range(res.GetNumberOfRows()):
        _, tv = res.GetValue(r, -1)
        times.append((t0 + timedelta(seconds=tv * CFG["time_unit_seconds"])).isoformat())
        for k, c in cols.items():
            series[k].append(res.GetValue(r, c)[1])
    res.Release()
    return {"time": times, "series": series}


def run_powerfactory():
    sys.path.append(CFG["pf_python_path"])
    import powerfactory as pf
    pfa = pf.GetApplication()
    if pfa is None:
        raise RuntimeError("PowerFactory returned no application. Is a licence available?")
    sc = CFG["scenarios"]
    base = None
    if sc["uncontrolled"].get("enabled"):      # first, so the MPC run's files are the ones left on disk
        base = run_scenario(pfa, sc["uncontrolled"], "uncontrolled")
    data = run_scenario(pfa, sc["mpc"], "MPC")
    data["baseline"] = base
    return data


# ---------- EV data written by the MPC script ----------
def read_ev():
    sess = list(csv.DictReader(open(OUT / "ev_summary_mpc.csv")))
    sch = list(csv.DictReader(open(OUT / "ev_schedule_mpc_MW.csv")))
    ids = [c[:-3] for c in sch[0] if c.endswith("_MW") and c not in ("PV_MW", "Load_MW")]
    schedule = {"time_h": [float(r["time_h"]) for r in sch]}
    for i in ids:  # kW, absolute (generator-convention EVs are negative in the file)
        schedule[i] = [abs(float(r[i + "_MW"])) * 1000 for r in sch]
    return rank(sess_to_list(sess)), schedule


def sess_to_list(rows):
    return [{"ev_id": r["ev_id"], "arrival": float(r["arrival_h"]), "departure": float(r["departure_h"]),
             "arrival_soc": round(float(r["arrival_soc"]) * 100), "target_soc": round(float(r["target_soc"]) * 100),
             "departure_soc": round(float(r["departure_soc"]) * 100), "urgency": float(r["urgency"])} for r in rows]


def rank(sessions):
    """Priority by urgency rank (the script's own measure: energy needed / energy deliverable in the window)."""
    sessions.sort(key=lambda s: -s["urgency"])
    labels = ["Critical", "High", "Medium", "Low"]
    n = len(sessions)
    for i, s in enumerate(sessions):
        s["priority"] = labels[min(3, i * 4 // n)]
    return sessions


# ---------- demo data ----------
def demo_results():
    t0 = datetime.fromisoformat(CFG["start_date"])
    times = [(t0 + timedelta(minutes=60 * i)).isoformat() for i in range(25)]
    g = lambda x, m, s: math.exp(-((x - m) ** 2) / (2 * s * s))
    h = range(25)
    res = [0.03 + 0.04 * g(x, 8, 1.5) + 0.08 * g(x, 19, 2) for x in h]
    pv = [0.07 * g(x, 12.5, 2.8) for x in h]
    ev_u = [0.09 * g(x, 18, 1.2) for x in h]          # uncontrolled: evening peak
    ev_c = [0.045 if 5 <= x <= 15 else 0 for x in h]  # MPC: spread over the PV hours
    mk = lambda ev: {
        "voltage": [1 - 0.12 * (r + e - p) for r, e, p in zip(res, ev, pv)],
        "loading": [min(100, abs(r + e - p) * 900) for r, e, p in zip(res, ev, pv)],
        "losses": [0.00002 * ((r + e - p) / 0.05) ** 2 for r, e, p in zip(res, ev, pv)],
        "residential": res, "pv": pv}
    sessions = rank([{"ev_id": f"EV_0{i+1}", "arrival": 4.0, "departure": 16.0, "arrival_soc": a, "target_soc": 75,
                      "departure_soc": 75, "urgency": u} for i, (a, u) in enumerate([(15, .14), (45, .07), (35, .09), (15, .14)])])
    schedule = {"time_h": list(range(24))}
    for s in sessions:
        schedule[s["ev_id"]] = [11.0 if 5 <= t <= 14 else 0 for t in range(24)]
    return {"time": times, "series": mk(ev_c), "baseline": {"time": times, "series": mk(ev_u)},
            "ev": {"sessions": sessions, "schedule": schedule}}


# ---------- API ----------
def worker():
    if not LOCK.acquire(blocking=False):
        return
    try:
        STATE.update(status="running", message="Starting...")
        if CFG["demo_mode"]:
            data = demo_results()
        else:
            data = run_powerfactory()
            sessions, schedule = read_ev()
            data["ev"] = {"sessions": sessions, "schedule": schedule}
        STATE.update(status="done", message="Run complete", results=data,
                     updated=datetime.now().isoformat(timespec="seconds"))
    except Exception as e:
        STATE.update(status="error", message=str(e))
    finally:
        LOCK.release()

@app.post("/api/run")
def run():
    if STATE["status"] == "running":
        return jsonify(STATE), 409
    STATE["status"] = "running"
    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"status": "running"})

@app.get("/api/results")
def results():
    return jsonify({**STATE, "demo": CFG["demo_mode"]})

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

if __name__ == "__main__":
    app.run(port=5000)
