import os
import csv
import math
import traceback

import powerfactory

app = powerfactory.GetApplication()


def log(msg):
    app.PrintPlain(str(msg))


# =====================================================================
# 0. USER SETTINGS
#    Run this script with the "Controlled/MPC" project ACTIVE.
# =====================================================================
EV_CLASS = "ElmLod"
EV_PREFIX = "EV_"

PV_NAME = "PV System"
PV_CLASS = "ElmPvsys"
LOAD_NAME = "Residential Load"

ATTACH_CHARACTERISTICS = True
RUN_QDS = True

OUTPUT_DIR = os.path.expanduser("~")
SCHEDULE_CSV = os.path.join(OUTPUT_DIR, "ev_schedule_mpc_MW.csv")

LOAD_CSV = os.path.join(OUTPUT_DIR, "residential_profile_digsilent.csv")
LOAD_CSV_HOUR_COL = "hour"
LOAD_CSV_VALUE_COL = "p_grid_kw"    # kW; NOT the 'residential' fraction column

PV_CSV = os.path.join(OUTPUT_DIR, "Solar_Temporary.csv")
PV_CSV_HOUR_COL = "Hour"
PV_CSV_VALUE_COL = "PV"             # kW

EV_ATTR, EV_SIGN = ("pgini", -1.0) if EV_CLASS == "ElmGenstat" else ("plini", +1.0)
PV_ATTR = "pgini"
LOAD_ATTR = "plini"

# ---- time resolution ---------------------------------------------------
# Must divide 60 (60, 30, 20, 15 ...). If you use anything but 60, set the
# step of the Quasi-Dynamic Simulation to the same value, otherwise
# PowerFactory samples the characteristics only once per QDS step.
STEP_MIN = 60

# ---- per-EV data (lists are reused cyclically if there are more EVs) ----
# Plug-in windows in hours of the day. Arrival must be earlier than departure
# (windows that cross midnight are not supported).
ARRIVAL_TIMES = [4, 4, 4, 4, 4, 4, 4, 4]

DEPARTURE_TIMES = [22, 22, 22, 22, 22, 22, 22, 22]

SOC0_LIST = [0.30, 0.45, 0.40, 0.50, 0.35, 0.60, 0.45, 0.40]

TARGET_SOC_LIST = [0.80, 0.85, 0.80, 0.90, 0.80, 0.90, 0.85, 0.80]

CAPACITY_KWH_LIST = [10*60.0, 10*60.0, 10*60.0, 10*60.0, 10*60.0, 10*60.0, 10*60.0, 10*60.0]
MAX_POWER_KW_LIST = [150.0, 150.0, 150.0, 150.0, 150.0, 150.0, 150.0, 150.0]
EFF_IDLE_KW = 0.0        # constant 90% efficiency, same as the uncontrolled script

# ---- charging goal ------------------------------------------------------
# Stage 1 guarantees TARGET_SOC_LIST (as far as the hard limits allow).
# Stage 2 then uses ALL remaining grid headroom to charge towards MAX_SOC,
# preferring PV hours (cheapest slots first). Set FILL_TO_MAX = False to
# stop at the targets.
FILL_TO_MAX = False      # True = keep charging past the target towards MAX_SOC
MAX_SOC = 1.0

# ---- charger efficiency -------------------------------------------------
# eff(p) = EFF_MAX * p / (p + EFF_IDLE_KW): lower efficiency at low power.
# Set EFF_IDLE_KW = 0 for a constant efficiency.
EFF_MAX = 0.90


# ---- grid constraints (hard limits) -------------------------------------
TRAFO_NAME = None            # e.g. "2-Winding Transformer"; None = first ElmTr2 found
TRAFO_KVA = None              # None = read the rating from the transformer (recommended)
TRAFO_FALLBACK_KVA = 1500.0   # used only if it cannot be read

# Scaling between the CSV values and what PowerFactory shows
LOAD_SCALE = 0.01
PV_SCALE = 0.01

LOADING_MAX = 0.98           # hard limit: transformer loading stays below this fraction
SOFT_LOADING = 0.95          # soft penalty starts here
LOAD_PF = 0.88               # fallbacks, the script tries to read the real values
EV_PF = 0.95

# ---- closed-loop correction against PowerFactory ------------------------
# The optimizer's model uses S = sqrt(P^2 + Q^2) at 1.0 p.u. PowerFactory's
# transformer loading is current based (I / I_rated) and also carries the
# network losses and reactive power behind the transformer, so at ~0.97 p.u.
# it reads several percent higher than the model. After each QDS run the
# script reads the real loading, tightens the limit only in the slots that
# went over, re-plans and re-runs until every slot is within LOADING_MAX.
MAX_ITER = 6                 # max. optimise -> QDS -> correct rounds
LOADING_TOL = 0.2            # % points above LOADING_MAX still accepted
CORR_MARGIN = 0.005          # extra tightening per correction (0.5 %)

# Simple voltage proxy: V = V0 - KAPPA/1000 * P_net[kW]
# KAPPA was calibrated from the PowerFactory results:
#   ~0.008 p.u. drop at ~255 kW and ~0.048 p.u. drop at ~1.6 MW  ->  ~0.031 p.u./MW
# (the old value 0.20 was ~6x too pessimistic and capped net power at 250 kW,
#  which is why the EVs stopped charging at ~17 % transformer loading)
V0 = 1.00
V_MIN = 0.95
KAPPA = 0.031

# ---- objective weights (soft preferences, ranked below the hard limits) --
W_IMPORT = 1.0       # cost of importing from the grid (PV surplus is free)
SOLAR_BIAS = 0.9     # 0..1: import costs up to this much less in the PV hours
W_FLAT = 0.1         # flatten the net profile (lower losses, fills valleys)
W_SOFT = 5.0         # penalty near the 95%~98% limit window to guard the hard constraint

RES_KW = 0.5         # allocation resolution of the optimizer [kW]; 1-2 kW runs 2-4x faster
PRINT_ALL_HOURS = False   # final schedule table: False = only hours with EV charging
NEED_TOL = 0.02      # energy tolerance when meeting the target [kWh]

# set in main()
DT = 1.0
NS = 24
S_MAX = None
P_NET_MAX = None
SOFT_S = None
S_LIM = None          # per-slot hard apparent-power limit [kVA] (corrected each round)
SOFT_LIM = None       # per-slot soft limit [kVA]
LO = 0.0
TAN_L = math.tan(math.acos(LOAD_PF))
TAN_EV = math.tan(math.acos(EV_PF))


# =====================================================================
# DASHBOARD EXPORT (results of the quasi-dynamic simulation -> CSV)
# =====================================================================
EXPORT_DIR = os.path.expanduser("~")
EXPORT_TAG = "mpc"              # -> results_mpc.csv   ("uncontrolled" -> results_uncontrolled.csv)
EXPORT_STEP_H = 1.0             # QDS step in hours (STEP_MIN / 60)
EXPORT_RESULT_NAME = ""         # name of the QDS result file (ElmRes); "" = auto-detect
# (key, "object name.Class", "result variable", scale)
EXPORT_SIGNALS = [
    ("voltage",     "load.ElmTerm",                     "m:u",       1.0),   # p.u.
    ("loading",     "2-Winding Transformer.ElmTr2",   "c:loading", 1.0),   # %
    ("losses",      "Grid.ElmNet",                     "c:LossP",   1.0),   # MW
    ("residential", "Residential Load.ElmLod",       "m:P:bus1",  1.0),   # MW
    ("pv",          "PV System.ElmPvsys",              "m:P:bus1",  1.0),   # MW (use -1.0 if PV shows negative)
]


def find_object(name):
    objs = app.GetCalcRelevantObjects(name)
    if not objs:                            # e.g. ElmNet is not "calc relevant"
        objs = app.GetProjectFolder("netmod").GetContents(name, 1)
    return objs[0] if objs else None


def find_results(qds):
    study = app.GetActiveStudyCase()
    if EXPORT_RESULT_NAME:
        found = study.GetContents(EXPORT_RESULT_NAME + ".ElmRes")
        return found[0] if found else None
    try:                                              # the result file the QDS command writes to
        r = qds.p_resvar
        if r is not None:
            return r
    except Exception:
        pass
    found = study.GetContents("*.ElmRes")
    return found[0] if found else None


def export_results(qds=None):
    """Write the QDS results to results_<tag>.csv for the dashboard."""
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
                log("WARNING: export - '%s' skipped (%s / %s not in result file %s)"
                    % (key, obj_name, var, res.loc_name))
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
        log("Results exported to %s (%d signals, %d rows)" % (path, len(cols), n_rows))
    except Exception:
        log("WARNING: export failed:\n" + traceback.format_exc())


def read_qds_loading(qds, trafo):
    """Return the transformer loading [%] per slot from the last QDS run (max within a slot)."""
    try:
        res = find_results(qds)
        if res is None:
            log("WARNING: no ElmRes result file found, cannot check loading")
            return None
        res.Load()
        try:
            c = res.FindColumn(trafo, "c:loading")
            if c < 0:
                log("WARNING: 'c:loading' of %s is not in result file %s; "
                    "add it to the QDS result variables" % (trafo.loc_name, res.loc_name))
                return None
            vals = [None] * NS
            for r in range(res.GetNumberOfRows()):
                t = int(r * EXPORT_STEP_H / DT + 1e-9)
                if t >= NS:
                    continue
                v = res.GetValue(r, c)[1]
                vals[t] = v if vals[t] is None else max(vals[t], v)
        finally:
            res.Release()
        missing = [t for t in range(NS) if vals[t] is None]
        if missing:
            log("WARNING: QDS results missing slot(s) %s" % [tlabel(t) for t in missing])
            return None
        return vals
    except Exception:
        log("WARNING: reading QDS loading failed:\n" + traceback.format_exc())
        return None


class EV:
    def __init__(self, name, soc0, cap, max_power, target, arrive, depart):
        self.name = name
        self.soc0 = soc0
        self.cap = cap                      # kWh
        self.max_power = max_power          # kW (grid side)
        self.target = target
        self.target_kwh = target * cap
        self.arrive = arrive                # slot index
        self.depart = depart                # slot index (exclusive)


# =====================================================================
# HELPERS
# =====================================================================
def list_names(cls):
    return [o.loc_name for o in app.GetCalcRelevantObjects("*." + cls)]


def find_one(name, cls):
    found = app.GetCalcRelevantObjects("%s.%s" % (name, cls))
    if not found:
        log("WARNING: '%s.%s' not found. Available %s objects: %s"
            % (name, cls, cls, list_names(cls)))
        return None
    return found[0]


def first(x):
    while isinstance(x, (list, tuple)):
        if not x:
            return None
        x = x[0]
    return x


def pick(lst, idx):
    return lst[idx % len(lst)]


def tlabel(t):
    m = int(round(t * STEP_MIN))
    return "%02d:%02d" % (m // 60, m % 60)


def get_trafo():
    try:
        if TRAFO_NAME:
            return app.GetCalcRelevantObjects(TRAFO_NAME + ".ElmTr2")[0]
        return app.GetCalcRelevantObjects("*.ElmTr2")[0]
    except Exception:
        return None


def read_trafo_kva():
    """Return (rating in kVA, name)."""
    try:
        if TRAFO_NAME:
            t = app.GetCalcRelevantObjects(TRAFO_NAME + ".ElmTr2")[0]
        else:
            trs = app.GetCalcRelevantObjects("*.ElmTr2")
            t = trs[0]
            if len(trs) > 1:
                log("WARNING: %d transformers found (%s); using '%s'. Set TRAFO_NAME to pick one."
                    % (len(trs), [x.loc_name for x in trs], t.loc_name))
        pf_kva = None
        try:
            n_par = getattr(t, "ntnum", 1) or 1
            pf_kva = t.typ_id.strn * 1000.0 * n_par
        except Exception:
            pass
        if TRAFO_KVA is not None:
            if pf_kva is not None and abs(pf_kva - TRAFO_KVA) > 0.01 * pf_kva:
                log("WARNING: TRAFO_KVA = %.0f kVA but %s is rated %.0f kVA in PowerFactory; "
                    "PowerFactory's loading uses %.0f kVA. Set TRAFO_KVA = None to use it."
                    % (TRAFO_KVA, t.loc_name, pf_kva, pf_kva))
            return TRAFO_KVA, t.loc_name + " (setting)"
        if pf_kva is None:
            raise RuntimeError("rating not readable")
        return pf_kva, t.loc_name
    except Exception:
        log("WARNING: could not read transformer, using %.0f kVA" % TRAFO_FALLBACK_KVA)
        return (TRAFO_KVA if TRAFO_KVA is not None else TRAFO_FALLBACK_KVA), "fallback"


def read_pf(elm, fallback, label):
    if elm is None:
        return fallback
    for attr in ("coslini", "cosn"):
        try:
            v = float(getattr(elm, attr))
            if 0.1 < v <= 1.0:
                log("    %s power factor from %s.%s = %.3f" % (label, elm.loc_name, attr, v))
                return v
        except Exception:
            pass
    log("    %s power factor not readable, using %.2f" % (label, fallback))
    return fallback


def make_char(folder, name, values_mw):
    existing = folder.GetContents(name + ".ChaTime")
    ch = first(existing) if existing else first(folder.CreateObject("ChaTime", name))
    ch.source = 0
    ch.repeat = 0
    ch.cunit = 0
    ch.stepSize = STEP_MIN
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
        if h_key is None or v_key is None:
            raise RuntimeError(
                "%s: expected columns '%s' and '%s', found %s"
                % (path, hour_col, value_col, reader.fieldnames)
            )
        for row in reader:
            h = int(float(row[h_key]))
            if 0 <= h <= 23:
                vals[h] = float(row[v_key])
    missing = [h for h, v in enumerate(vals) if v is None]
    if missing:
        raise RuntimeError("%s missing hour(s): %s" % (path, missing))
    return vals


def to_slots(hourly):
    """Hold each hourly value over all slots of that hour."""
    return [hourly[min(23, int(t * DT + 1e-9))] for t in range(NS)]


# =====================================================================
# MODELS
# =====================================================================
def eff(p):
    if p <= 0:
        return 0.0
    if EFF_IDLE_KW <= 0:
        return EFF_MAX
    return EFF_MAX * p / (p + EFF_IDLE_KW)


def gain_kwh(p):
    """Energy stored in the battery in one slot at grid-side power p [kW]."""
    return eff(p) * p * DT


def s_headroom(p, q, smax):
    """Largest extra EV power x >= 0 [kW] with sqrt((p+x)^2 + (q+TAN_EV*x)^2) <= smax."""
    a = 1.0 + TAN_EV * TAN_EV
    b = 2.0 * (p + q * TAN_EV)
    c = p * p + q * q - smax * smax
    if c > 0.0:            # only clamp if ALREADY over the limit
        return 0.0

    disc = b * b - 4.0 * a * c
    if disc < 0:
        return 0.0
    return max(0.0, (-b + math.sqrt(disc)) / (2.0 * a))


# =====================================================================
# MPC
# =====================================================================
def mpc_step(k, evs, socs, plan, base_p_s, base_q_s, pv_frac):
    """Plan slots k..NS-1 and return the EV powers for slot k only."""
    n = len(evs)
    alloc = [[0.0] * NS for _ in range(n)]
    for t in range(k):
        for i in range(n):
            alloc[i][t] = plan[t][i]
    net = [base_p_s[t] + sum(alloc[i][t] for i in range(n)) for t in range(NS)]
    q = [base_q_s[t] + TAN_EV * sum(alloc[i][t] for i in range(n)) for t in range(NS)]
    need = [max(0.0, evs[i].target_kwh - socs[i]) if k < evs[i].depart else 0.0
            for i in range(n)]

    # Grid headroom per slot is the same for every EV, so it is computed once
    # per slot and only refreshed for the slot that just received power.
    grid_room = [0.0] * NS

    def update_room(t):
        grid_room[t] = min(P_NET_MAX - net[t], s_headroom(net[t], q[t], S_LIM[t]))

    for t in range(k, NS):
        update_room(t)

    def headroom(i, t):
        h = min(evs[i].max_power - alloc[i][t], grid_room[t])
        return h if h > 0 else 0.0

    def urgency(i):
        e = evs[i]
        cap = 0.0
        for t in range(max(k, e.arrive), e.depart):
            cap += headroom(i, t)
        return need[i] / (cap * DT * EFF_MAX + 1e-6)

    def cost(t, dp):
        n0 = net[t]
        n1 = n0 + dp
        c = W_IMPORT * (1.0 - SOLAR_BIAS * pv_frac[t]) * (max(0.0, n1) - max(0.0, n0))
        c += W_FLAT * ((n1 - LO) ** 2 - (n0 - LO) ** 2) / S_MAX
        s0 = math.hypot(n0, q[t])
        s1 = math.hypot(n1, q[t] + TAN_EV * dp)
        c += W_SOFT * (max(0.0, s1 - SOFT_LIM[t]) - max(0.0, s0 - SOFT_LIM[t]))
        return max(c, 1e-12)

    def best_slot(i):
        e = evs[i]
        best = None
        best_score = None

        # Deadline-adaptive urgency scaler
        time_left = max(1, e.depart - k)
        urgency_multiplier = max(1.0, need[i] / (time_left * e.max_power * DT * EFF_MAX + 1e-6))

        for t in range(max(k, e.arrive), e.depart):
            dp = min(RES_KW, headroom(i, t))
            if dp < 1e-6:
                continue
            p0 = alloc[i][t]
            g = gain_kwh(p0 + dp) - gain_kwh(p0)
            if g <= 1e-9:
                continue
            if g > need[i]:                    # do not overshoot the goal
                dp *= need[i] / g
                g = gain_kwh(p0 + dp) - gain_kwh(p0)
                if dp < 1e-6 or g <= 1e-9:
                    continue

            # Scale down cost penalization when deadline urgency grows
            score = (cost(t, dp) / urgency_multiplier) / g

            if best_score is None or score < best_score:
                best, best_score = (t, dp, g), score
        return best

    def fill(active):
        """Greedy allocation: repeatedly add RES_KW to the cheapest slot of the most urgent EV."""
        guard = 0
        while active and guard < 500000:
            guard += 1
            i = max(active, key=urgency)
            res = best_slot(i)
            if res is None:                    # no feasible slot: stays short
                active.remove(i)
                continue
            t, dp, g = res
            alloc[i][t] += dp
            net[t] += dp
            q[t] += TAN_EV * dp
            update_room(t)
            need[i] -= g
            if need[i] <= NEED_TOL:
                active.remove(i)

    # Stage 1: guarantee the requested target SOC first
    fill([i for i in range(n) if need[i] > NEED_TOL])

    # Stage 2: use all remaining headroom to get as full as possible.
    # Cheapest slots are filled first, so PV hours are preferred; EVs whose
    # window has no PV simply use whatever grid headroom is left.
    if FILL_TO_MAX:
        for i in range(n):
            if k < evs[i].depart:
                planned = sum(gain_kwh(alloc[i][t]) for t in range(k, NS))
                need[i] = max(0.0, MAX_SOC * evs[i].cap - socs[i] - planned)
            else:
                need[i] = 0.0
        fill([i for i in range(n) if need[i] > NEED_TOL])

    return [alloc[i][k] for i in range(n)]


def mpc_schedule(evs, base_p_s, base_q_s, pv_frac):
    """Receding horizon: re-solve every slot with the current SOC, apply only the first action."""
    socs = [e.soc0 * e.cap for e in evs]
    sched = [[0.0] * len(evs) for _ in range(NS)]
    dep_soc = {}
    for k in range(NS):
        # No EV plugged in at slot k -> nothing to decide; the full re-plan
        # would return zeros anyway, so skip it (same result, much faster).
        if any(e.arrive <= k < e.depart for e in evs):
            sched[k] = mpc_step(k, evs, socs, sched, base_p_s, base_q_s, pv_frac)
        for i, e in enumerate(evs):
            socs[i] = min(e.cap, socs[i] + gain_kwh(sched[k][i]))
            if k + 1 == e.depart:
                dep_soc[i] = socs[i] / e.cap
    return sched, dep_soc, [socs[i] / evs[i].cap for i in range(len(evs))]


def uncontrolled_schedule(evs):
    """Reference: every EV charges at full power from arrival until its target is reached."""
    sched = [[0.0] * len(evs) for _ in range(NS)]
    for i, e in enumerate(evs):
        soc = e.soc0 * e.cap
        for t in range(e.arrive, e.depart):
            need = e.target_kwh - soc
            if need <= NEED_TOL:
                break
            p = e.max_power
            g = gain_kwh(p)
            if g > need:
                p *= need / g
                g = gain_kwh(p)
            sched[t][i] = p
            soc += g
    return sched


def model_s(sched, base_p_s, base_q_s, t):
    ev = sum(sched[t])
    p = base_p_s[t] + ev
    return p, math.hypot(p, base_q_s[t] + TAN_EV * ev)


def evaluate(label, sched, base_p_s, base_q_s, pv_s, kva):
    peak_s, peak_t, max_p = 0.0, 0, -1e9
    ev_e = from_pv = in_sun = imp = 0.0
    for t in range(NS):
        ev = sum(sched[t])
        p = base_p_s[t] + ev
        qk = base_q_s[t] + TAN_EV * ev
        s = math.hypot(p, qk)
        if s > peak_s:
            peak_s, peak_t = s, t
        max_p = max(max_p, p)
        imp += max(0.0, p) * DT
        ev_e += ev * DT
        from_pv += min(ev, max(0.0, -base_p_s[t])) * DT
        if pv_s[t] > 1e-9:
            in_sun += ev * DT
    ev_e_safe = max(ev_e, 1e-9)
    log("%-12s peak %.1f kVA (%.0f%%) at %s | min V %.3f p.u. | grid import %.0f kWh | "
        "EV energy %.0f kWh: %.0f%% in PV hours, %.0f%% covered by PV surplus"
        % (label, peak_s, 100.0 * peak_s / kva, tlabel(peak_t),
            V0 - KAPPA / 1000.0 * max_p, imp, ev_e,
            100.0 * in_sun / ev_e_safe, 100.0 * from_pv / ev_e_safe))


# =====================================================================
# MAIN
# =====================================================================
def main():
    global DT, NS, S_MAX, P_NET_MAX, SOFT_S, S_LIM, SOFT_LIM, LO, TAN_L, TAN_EV

    if 60 % STEP_MIN != 0:
        raise RuntimeError("STEP_MIN must divide 60")
    DT = STEP_MIN / 60.0
    NS = int(round(24.0 / DT))
    log("Step 1: connected to PowerFactory (MPC controlled scenario), %d min steps" % STEP_MIN)
    if STEP_MIN != 60:
        log("    NOTE: set the Quasi-Dynamic Simulation step size to %d min as well" % STEP_MIN)

    pf_evs = sorted(app.GetCalcRelevantObjects("%s*.%s" % (EV_PREFIX, EV_CLASS)),
                    key=lambda o: o.loc_name)
    if not pf_evs:
        raise RuntimeError("No '%s*.%s' objects found in the active project. "
                           "Make sure the Controlled project is active. "
                           "Available %s objects: %s"
                           % (EV_PREFIX, EV_CLASS, EV_CLASS, list_names(EV_CLASS)))
    pv_gen = find_one(PV_NAME, PV_CLASS)
    base_load = find_one(LOAD_NAME, "ElmLod")

    evs = []
    for idx, obj in enumerate(pf_evs):
        arr_h = pick(ARRIVAL_TIMES, idx)
        dep_h = pick(DEPARTURE_TIMES, idx)
        a = int(round(arr_h / DT))
        d = int(round(dep_h / DT))
        if not (0 <= a < d <= NS):
            raise RuntimeError("%s: arrival %s h must be earlier than departure %s h"
                               % (obj.loc_name, arr_h, dep_h))
        e = EV(obj.loc_name, pick(SOC0_LIST, idx), pick(CAPACITY_KWH_LIST, idx),
               pick(MAX_POWER_KW_LIST, idx), pick(TARGET_SOC_LIST, idx), a, d)
        evs.append(e)
        log("    EV [%d]: %s  %02d:00-%02d:00, SOC %.0f%% -> %.0f%% (max %.0f%%), %.0f kWh, %.0f kW"
            % (idx, e.name, arr_h, dep_h, e.soc0 * 100, e.target * 100,
               (MAX_SOC if FILL_TO_MAX else e.target) * 100, e.cap, e.max_power))
    log("Step 2: found %d EV object(s)" % len(evs))

    pv_hourly = read_hourly_csv(PV_CSV, PV_CSV_HOUR_COL, PV_CSV_VALUE_COL)
    load_hourly = read_hourly_csv(LOAD_CSV, LOAD_CSV_HOUR_COL, LOAD_CSV_VALUE_COL)
    pv_raw = to_slots(pv_hourly)       # kW, as in the CSV
    load_raw = to_slots(load_hourly)
    log("Step 3: loaded PV forecast (peak %.1f kW) and load forecast (peak %.1f kW)"
        % (max(pv_hourly), max(load_hourly)))

    load_pf = read_pf(base_load, LOAD_PF, "load")
    ev_pf = read_pf(pf_evs[0], EV_PF, "EV")
    TAN_L = math.tan(math.acos(load_pf))
    TAN_EV = math.tan(math.acos(ev_pf))

    kva, tr_name = read_trafo_kva()
    trafo = get_trafo()
    S_MAX = LOADING_MAX * kva
    SOFT_S = SOFT_LOADING * kva
    S_LIM = [S_MAX] * NS
    SOFT_LIM = [SOFT_S] * NS
    P_NET_MAX = (V0 - V_MIN) / (KAPPA / 1000.0)
    log("    transformer %s: %.1f kVA -> hard limit %.1f kVA (%.0f%%), soft limit %.1f kVA, "
        "voltage P limit %.1f kW" % (tr_name, kva, S_MAX, LOADING_MAX * 100, SOFT_S, P_NET_MAX))

    # what PowerFactory will actually see (after element scaling)
    pv_s = [x * PV_SCALE for x in pv_raw]
    base_p_s = [load_raw[t] * LOAD_SCALE - pv_s[t] for t in range(NS)]
    base_q_s = [load_raw[t] * LOAD_SCALE * TAN_L for t in range(NS)]
    pv_peak = max(max(pv_s), 1e-9)
    pv_frac = [max(0.0, x) / pv_peak for x in pv_s]
    LO = min(base_p_s)

    for t in range(NS):
        s0 = math.hypot(base_p_s[t], base_q_s[t])
        if s0 > S_MAX:
            log("WARNING: %s base load alone is %.1f kVA (%.0f%%) > limit; EVs get no power then"
                % (tlabel(t), s0, 100 * s0 / kva))

    # ---- one-time setup in PowerFactory: load + PV characteristics ----
    opchar = None
    qds = None
    if ATTACH_CHARACTERISTICS:
        oplib = app.GetProjectFolder("oplib")
        opchar = first(oplib.GetContents("Characteristics"))
        if opchar is None:
            raise RuntimeError("Operational Library has no 'Characteristics' folder")
        if base_load is not None:
            ch = make_char(opchar, "MPC_Load", [x / 1000.0 for x in load_raw])
            link_char(base_load, LOAD_ATTR, ch)
            log("    %s.%s -> %s" % (base_load.loc_name, LOAD_ATTR, ch.loc_name))
        if pv_gen is not None:
            ch = make_char(opchar, "MPC_PV", [x / 1000.0 for x in pv_raw])
            link_char(pv_gen, PV_ATTR, ch)
            log("    %s.%s -> %s" % (pv_gen.loc_name, PV_ATTR, ch.loc_name))
    if RUN_QDS:
        qds = app.GetFromStudyCase("ComStatsim")
        if qds is None:
            log("WARNING: no Quasi-Dynamic Simulation command in the study case")

    closed_loop = ATTACH_CHARACTERISTICS and qds is not None and trafo is not None
    if ATTACH_CHARACTERISTICS and qds is not None and trafo is None:
        log("WARNING: transformer not found, closed-loop loading check disabled")
    limit_pct = LOADING_MAX * 100.0

    # ---- optimise -> simulate -> correct ------------------------------
    sched = dep_soc = final_socs = None
    qds_ok = False
    for it in range(1, (MAX_ITER if closed_loop else 1) + 1):
        log("Step 4 (round %d): computing MPC schedule" % it)
        sched, dep_soc, final_socs = mpc_schedule(evs, base_p_s, base_q_s, pv_frac)

        if ATTACH_CHARACTERISTICS:
            for i, obj in enumerate(pf_evs):
                ch = make_char(opchar, "MPC_" + obj.loc_name,
                               [EV_SIGN * sched[t][i] / 1000.0 for t in range(NS)])
                link_char(obj, EV_ATTR, ch)
            log("    EV characteristics attached (%d)" % len(pf_evs))

        if qds is None:
            break
        log("Step 5 (round %d): running quasi-dynamic simulation" % it)
        err = qds.Execute()
        qds_ok = (err == 0)
        if not qds_ok:
            log("QDS failed (code %s)" % err)
            break
        if not closed_loop:
            break

        actual = read_qds_loading(qds, trafo)
        if actual is None:
            log("WARNING: could not read transformer loading, stopping the correction loop")
            break

        over, stuck = [], []
        for t in range(NS):
            _, s = model_s(sched, base_p_s, base_q_s, t)
            if actual[t] > limit_pct + LOADING_TOL:
                if sum(sched[t]) > 1e-6:
                    over.append(t)
                else:
                    stuck.append(t)
            if sum(sched[t]) > 1e-6 or actual[t] > limit_pct:
                log("    %s model %.1f%%  PowerFactory %.1f%%%s"
                    % (tlabel(t), 100 * s / kva, actual[t],
                       "  <-- OVER" if actual[t] > limit_pct + LOADING_TOL else ""))
        for t in stuck:
            log("WARNING: %s is at %.1f%% with no EV charging; the base load alone exceeds the limit"
                % (tlabel(t), actual[t]))

        if not over:
            log("Loading check passed: every EV slot is within %.1f%% in PowerFactory" % limit_pct)
            break

        # Calibrate the model: ratio = PowerFactory loading / model loading.
        # The limit is set directly from the measured ratio (not nudged), so
        # it binds even when the model under-reads by a large factor.
        ratios = {}
        for t in range(NS):
            if sum(sched[t]) > 1e-6:
                m_pct = 100.0 * model_s(sched, base_p_s, base_q_s, t)[1] / kva
                if m_pct > 1.0:
                    ratios[t] = actual[t] / m_pct
        worst = max(ratios.values()) if ratios else 1.0
        log("    PowerFactory reads %.2fx the model (worst charging hour)" % worst)
        if worst > 1.3:
            log("WARNING: PowerFactory loading is %.1fx the model. Check TRAFO_KVA (%.0f kVA used) "
                "against the transformer's real rating, and the load/EV scaling factors."
                % (worst, kva))

        changed = False
        for t in range(NS):
            r = max(1.0, ratios.get(t, worst))
            new_lim = S_MAX * (1.0 - CORR_MARGIN) / r
            if new_lim < S_LIM[t] - 1e-6:
                S_LIM[t] = new_lim
                SOFT_LIM[t] = new_lim * SOFT_LOADING / LOADING_MAX
                changed = True
        for t in over:
            log("    %s: %.1f%% in PowerFactory -> model limit set to %.1f%%"
                % (tlabel(t), actual[t], 100 * S_LIM[t] / kva))
        if not changed:
            log("WARNING: limits cannot be tightened further; stopping the correction loop")
            break
        if it == (MAX_ITER if closed_loop else 1):
            log("WARNING: still over the limit after %d rounds; increase MAX_ITER or CORR_MARGIN"
                % it)

    # ---- reports for the final schedule --------------------------------
    log("--- Final schedule ---")
    for t in range(NS):
        if sum(sched[t]) <= 1e-6 and not PRINT_ALL_HOURS:
            continue
        row = ", ".join("%s: %.1f" % (evs[i].name, sched[t][i]) for i in range(len(evs)))
        p, s = model_s(sched, base_p_s, base_q_s, t)
        log("%s | %s kW | P %.1f kW, S %.1f kVA = %.0f%% (limit %.1f%%)"
            % (tlabel(t), row, p, s, 100 * s / kva, 100 * S_LIM[t] / kva))

    log("--- Departure SOC vs target (MPC) ---")
    for i, e in enumerate(evs):
        s = dep_soc.get(i, final_socs[i])
        log("%s: %.2f (target %.2f, max %.2f) [%s]"
            % (e.name, s, e.target, MAX_SOC if FILL_TO_MAX else e.target,
               "OK" if s >= e.target - 0.001 else "SHORT"))

    log("--- Comparison (same forecast, optimizer model) ---")
    evaluate("uncontrolled", uncontrolled_schedule(evs), base_p_s, base_q_s, pv_s, kva)
    evaluate("MPC", sched, base_p_s, base_q_s, pv_s, kva)

    try:
        with open(SCHEDULE_CSV, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time_h", "PV_MW", "Load_MW"] + [e.name + "_MW" for e in evs])
            for t in range(NS):
                w.writerow(["%.4f" % (t * DT), "%.6f" % (pv_raw[t] / 1000.0),
                            "%.6f" % (load_raw[t] / 1000.0)]
                           + ["%.6f" % (EV_SIGN * sched[t][i] / 1000.0) for i in range(len(evs))])
        log("Step 6: schedule written to %s" % SCHEDULE_CSV)
    except Exception as ex:
        log("WARNING: could not write CSV (%s)" % ex)

    # per-EV summary for the dashboard
    try:
        summary_csv = os.path.join(OUTPUT_DIR, "ev_summary_mpc.csv")
        with open(summary_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ev_id", "arrival_h", "departure_h", "arrival_soc",
                        "target_soc", "departure_soc", "urgency"])
            for i, e in enumerate(evs):
                window_kwh = e.max_power * (e.depart - e.arrive) * DT * EFF_MAX
                urg = max(0.0, e.target_kwh - e.soc0 * e.cap) / max(window_kwh, 1e-6)
                w.writerow([e.name, "%.2f" % (e.arrive * DT), "%.2f" % (e.depart * DT),
                            "%.3f" % e.soc0, "%.3f" % e.target,
                            "%.3f" % dep_soc.get(i, final_socs[i]), "%.4f" % urg])
        log("Step 6b: EV summary written to %s" % summary_csv)
    except Exception as ex:
        log("WARNING: could not write EV summary (%s)" % ex)

    if qds_ok:
        export_results(qds)

    log("Done.")


try:
    main()
except Exception:
    app.PrintError("Script failed:\n" + traceback.format_exc())