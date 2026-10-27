# Paste this block into your MPC script inside main(), right AFTER the
# "Step 5: schedule written ..." try/except block (before "if ATTACH_CHARACTERISTICS").
# It writes the per-EV summary the dashboard needs. Nothing else in your script changes.

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
        log("Step 5b: EV summary written to %s" % summary_csv)
    except Exception as ex:
        log("WARNING: could not write EV summary (%s)" % ex)
