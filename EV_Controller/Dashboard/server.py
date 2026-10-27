"""EV Charging & Grid Monitor - backend.
Reads the CSV files your PowerFactory scripts export and serves the dashboard.
Run:  python server.py   ->  http://localhost:5000
Run it as the same Windows user that runs the MPC script (it reads/writes files in ~).
"""
import csv, json, math
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify, send_from_directory

BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text())
OUT = Path(CFG["data_dir"] or Path.home())
app = Flask(__name__, static_folder=str(BASE / "static"))


# ---------- results files written by the PowerFactory scripts ----------
def read_series(path):
    rows = list(csv.DictReader(open(path)))
    t0 = datetime.fromisoformat(CFG["start_date"])
    times = [(t0 + timedelta(hours=float(r["time_h"]))).isoformat() for r in rows]
    keys = [k for k in rows[0] if k not in ("time_h", "time_raw")]
    return {"time": times, "series": {k: [float(r[k]) if r[k] not in ("", None) else None for r in rows] for k in keys}}


def load_results():
    mpc = OUT / "results_mpc.csv"
    need = [mpc, OUT / "ev_summary_mpc.csv", OUT / "ev_schedule_mpc_MW.csv"]
    missing = [p.name for p in need if not p.exists()]
    if missing:
        raise FileNotFoundError(f"Waiting for files in {OUT}: {', '.join(missing)}. Run the MPC script in PowerFactory first.")
    data = read_series(mpc)
    unc = OUT / "results_uncontrolled.csv"
    data["baseline"] = read_series(unc) if unc.exists() else None
    sessions, schedule = read_ev()
    data["ev"] = {"sessions": sessions, "schedule": schedule}
    files = need + ([unc] if unc.exists() else [])
    data["updated"] = datetime.fromtimestamp(max(p.stat().st_mtime for p in files)).isoformat(timespec="seconds")
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
@app.get("/api/results")
def results():
    try:
        data = demo_results() if CFG["demo_mode"] else load_results()
        data.setdefault("updated", datetime.now().isoformat(timespec="seconds"))
        return jsonify({"status": "done", "message": "", "demo": CFG["demo_mode"],
                        "updated": data["updated"], "results": data})
    except FileNotFoundError as e:
        return jsonify({"status": "waiting", "message": str(e), "demo": False})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Could not read results: {e}", "demo": False})

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

if __name__ == "__main__":
    print(f"Reading results from {OUT}")
    app.run(port=5000)
