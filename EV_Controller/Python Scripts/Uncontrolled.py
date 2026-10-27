import os
import csv
import traceback

import powerfactory

app = powerfactory.GetApplication()


def log(msg):
    app.PrintPlain(str(msg))


# =====================================================================
# 0. USER SETTINGS
# =====================================================================
EV_CLASS = "ElmLod"
EV_PREFIX = "EV_"

PV_NAME = "PV System"
PV_CLASS = "ElmPvsys"
LOAD_NAME = "Residential Load"

ATTACH_CHARACTERISTICS = True
RUN_QDS = True

OUTPUT_DIR = os.path.expanduser("~")
SCHEDULE_CSV = os.path.join(OUTPUT_DIR, "ev_schedule_uncontrolled_MW.csv")

LOAD_CSV = os.path.join(OUTPUT_DIR, "residential_profile_digsilent.csv")
LOAD_CSV_HOUR_COL = "hour"
LOAD_CSV_VALUE_COL = "p_grid_kw"    # kW

PV_CSV = os.path.join(OUTPUT_DIR, "Solar_Temporary.csv")
PV_CSV_HOUR_COL = "Hour"
PV_CSV_VALUE_COL = "PV"             # kW

EV_ATTR, EV_SIGN = ("pgini", -1.0) if EV_CLASS == "ElmGenstat" else ("plini", +1.0)
PV_ATTR = "pgini"
LOAD_ATTR = "plini"

EFF = 0.9
T_STEP = 1.0  # h

# Arrival and Departure times (supports overnight: if depart <= arrive, it's next day)
ARRIVAL_TIMES = [4, 4, 4, 4, 4, 4, 4, 4]

DEPARTURE_TIMES = [22, 22, 22, 22, 22, 22, 22, 22]

SOC0_LIST = [0.30, 0.45, 0.40, 0.50, 0.35, 0.60, 0.45, 0.40]

TARGET_SOC_LIST = [0.80, 0.85, 0.80, 0.90, 0.80, 0.90, 0.85, 0.80]
CAPACITY_KWH = 10 * 60.0
MAX_POWER_KW = 150.0


# =====================================================================
# DASHBOARD EXPORT
# =====================================================================
EXPORT_DIR = os.path.expanduser("~")
EXPORT_TAG = "uncontrolled"
EXPORT_STEP_H = 1
EXPORT_RESULT_NAME = ""
EXPORT_SIGNALS = [
    ("voltage",     "load.ElmTerm",                    "m:u",       1.0),   # p.u.
    ("loading",     "2-Winding Transformer.ElmTr2",  "c:loading", 1.0),   # %
    ("losses",      "Grid.ElmNet",                     "c:LossP",   1.0),   # MW
    ("residential", "Residential Load.ElmLod",         "m:P:bus1",  1.0),   # MW
    ("pv",          "PV System.ElmPvsys",              "m:P:bus1",  1.0),   # MW
]


def find_object(name):
    objs = app.GetCalcRelevantObjects(name)
    if not objs:
        objs = app.GetProjectFolder("netmod").GetContents(name, 1)
    return objs[0] if objs else None


def find_results(qds):
    study = app.GetActiveStudyCase()
    cands = []
    if EXPORT_RESULT_NAME:
        cands = list(study.GetContents(EXPORT_RESULT_NAME + ".ElmRes"))
    else:
        try:
            r = qds.p_resvar
            if r is not None:
                cands.append(r)
        except Exception:
            pass
        seen = [c.loc_name for c in cands]
        cands += [r for r in study.GetContents("*.ElmRes") if r.loc_name not in seen]
    best, best_n = None, -1
    for r in cands:
        try:
            r.Load()
            n = r.GetNumberOfRows()
        except Exception:
            continue
        if n > best_n:
            best, best_n = r, n
    return best


def export_results(qds=None):
    try:
        res = find_results(qds)
        if res is None:
            log("WARNING: export skipped, no ElmRes result file found")
            return
        res.Load()
        cols = []
        for key, obj_name, var, scale in EXPORT_SIGNALS:
            obj = find_object(obj_name)
            c = res.FindColumn(obj, var) if obj is not None else -1
            if c < 0:
                log("WARNING: export - '%s' skipped" % key)
            else:
                cols.append((key, c, scale))
        path = os.path.join(EXPORT_DIR, "results_%s.csv" % EXPORT_TAG)
        n_rows = res.GetNumberOfRows()
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time_h", "time_raw"] + [k for k, _, _ in cols])
            for r in range(n_rows):
                w.writerow(["%.4f" % (r * EXPORT_STEP_H), res.GetValue(r, -1)[1]]
                           + [res.GetValue(r, c)[1] * s for _, c, s in cols])
        res.Release()
        log("Results exported to %s" % path)
    except Exception:
        log("WARNING: export failed:\n" + traceback.format_exc())


class EV:
    def __init__(self, name, soc0, capacity, max_power, target, arrive, depart):
        self.name = name
        self.soc0 = soc0
        self.capacity = capacity
        self.max_power = max_power
        self.target = target
        self.arrive = arrive
        # Handle overnight departure (e.g., arrive 16, depart 7 -> depart is 31 absolute hours)
        self.depart = depart + 24 if depart <= arrive else depart


# =====================================================================
# HELPERS
# =====================================================================
def list_names(cls):
    return [o.loc_name for o in app.GetCalcRelevantObjects("*." + cls)]


def find_one(name, cls):
    found = app.GetCalcRelevantObjects("%s.%s" % (name, cls))
    if not found:
        log("WARNING: '%s.%s' not found." % (name, cls))
        return None
    return found[0]


def first(x):
    while isinstance(x, (list, tuple)):
        if not x:
            return None
        x = x[0]
    return x


def make_char(folder, name, values_mw):
    existing = folder.GetContents(name + ".ChaTime")
    ch = first(existing) if existing else first(folder.CreateObject("ChaTime", name))
    ch.source = 0
    ch.repeat = 0
    ch.cunit = 0
    ch.stepSize = 60
    ch.usage = 2
    ch.approx = 4
    ch.SetAttribute("vector", [float(v) for v in values_mw])
    return ch


def link_char(elm, attr, ch):
    for r in elm.GetContents("*.ChaRef"):
        if r.loc_name == attr:
            r.Delete()
    ref = first(elm.CreateObject("ChaRef", attr))
    ref.typ_id = ch


def read_hourly_csv(path, hour_col, value_col):
    vals = [None] * 24
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        colmap = {k.strip().lower(): k for k in reader.fieldnames}
        h_key = colmap.get(hour_col.lower())
        v_key = colmap.get(value_col.lower())
        for row in reader:
            h = int(float(row[h_key]))
            if 0 <= h <= 23:
                vals[h] = float(row[v_key])
    return vals


def uncontrolled_schedule(evs, hours):
    """Each EV charges at full power from arrival until full or departure, supporting overnight."""
    socs = [e.soc0 for e in evs]
    sched = [[0.0] * len(evs) for _ in hours]
    dep_soc = {}
    
    # Simulate over an extended timeline (up to 48 hours to capture overnight)
    max_hr = max(e.depart for e in evs)
    total_hours = max(24, max_hr)
    
    full_sched = [[0.0] * len(evs) for _ in range(total_hours)]
    
    for k in range(total_hours):
        for i, e in enumerate(evs):
            if e.arrive <= k < e.depart and socs[i] < e.target - 1e-6:
                p = e.max_power
                gain = EFF * p * T_STEP / e.capacity
                if socs[i] + gain > e.target:
                    p *= (e.target - socs[i]) / gain
                    gain = e.target - socs[i]
                
                # Map back to the 24-hour daily profile vector for PowerFactory
                day_hour = k % 24
                full_sched[day_hour][i] += p
                socs[i] += gain
                
            if k + 1 == e.depart:
                dep_soc[i] = socs[i]
                
    return full_sched[:24], dep_soc, socs


# =====================================================================
# MAIN
# =====================================================================
def main():
    log("Step 1: connected to PowerFactory (uncontrolled baseline)")

    pf_evs = sorted(app.GetCalcRelevantObjects("%s*.%s" % (EV_PREFIX, EV_CLASS)),
                    key=lambda o: o.loc_name)
    if not pf_evs:
        raise RuntimeError("No '%s*.%s' objects found." % (EV_PREFIX, EV_CLASS))
    pv_gen = find_one(PV_NAME, PV_CLASS)
    base_load = find_one(LOAD_NAME, "ElmLod")

    evs = []
    for idx, obj in enumerate(pf_evs):
        arrive = ARRIVAL_TIMES[idx % len(ARRIVAL_TIMES)]
        depart = DEPARTURE_TIMES[idx % len(DEPARTURE_TIMES)]
        soc0 = SOC0_LIST[idx % len(SOC0_LIST)]
        target = TARGET_SOC_LIST[idx % len(TARGET_SOC_LIST)]
        evs.append(EV(obj.loc_name, soc0, CAPACITY_KWH, MAX_POWER_KW, target,
                      arrive, depart))

    hours = list(range(24))
    pv_fc = read_hourly_csv(PV_CSV, PV_CSV_HOUR_COL, PV_CSV_VALUE_COL)
    load_fc = read_hourly_csv(LOAD_CSV, LOAD_CSV_HOUR_COL, LOAD_CSV_VALUE_COL)

    sched, dep_soc, final_socs = uncontrolled_schedule(evs, hours)

    # ---- CSV export
    try:
        with open(SCHEDULE_CSV, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["hour", "PV_MW", "Load_MW"] + [e.name + "_MW" for e in evs])
            for k in hours:
                w.writerow([k, "%.6f" % (pv_fc[k] / 1000.0), "%.6f" % (load_fc[k] / 1000.0)]
                           + ["%.6f" % (EV_SIGN * sched[k][i] / 1000.0) for i in range(len(evs))])
        log("Step 5: schedule written to %s" % SCHEDULE_CSV)
    except Exception as ex:
        log("WARNING: could not write CSV (%s)" % ex)

    # ---- Attach characteristics
    if ATTACH_CHARACTERISTICS:
        oplib = app.GetProjectFolder("oplib")
        opchar = first(oplib.GetContents("Characteristics"))

        for i, obj in enumerate(pf_evs):
            ch = make_char(opchar, "UNC_" + obj.loc_name,
                           [EV_SIGN * sched[k][i] / 1000.0 for k in hours])
            link_char(obj, EV_ATTR, ch)

        if base_load is not None:
            ch = make_char(opchar, "UNC_Load", [x / 1000.0 for x in load_fc])
            link_char(base_load, LOAD_ATTR, ch)
        if pv_gen is not None:
            ch = make_char(opchar, "UNC_PV", [x / 1000.0 for x in pv_fc])
            link_char(pv_gen, PV_ATTR, ch)

    # ---- Run QDS
    if RUN_QDS:
        qds = app.GetFromStudyCase("ComStatsim")
        if qds is not None:
            err = qds.Execute()
            if err == 0:
                export_results(qds)

    log("Done.")


try:
    main()
except Exception:
    app.PrintError("Script failed:\n" + traceback.format_exc())