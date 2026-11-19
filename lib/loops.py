#!/usr/bin/env python3
"""Per-loop t-route timings from troute logs: results/<batch>/runs.csv -> loops.csv.

  loops.py BATCH_DIR

t-route runs a simulation in loops of max_loop_size hours. Per loop its log has
  lateral inflow DataFrame creation   reading the loop's forcing (qlat)
  JIT Preprocessing time              inside MC computation
  PARALLEL TIME                       inside MC computation: the routing itself
  MC computation                      routing of the loop
  output                              writing the loop's output file
in this order: forcing(0), MC(0), forcing(1), DA update, output(0), MC(1), forcing(2), ...
Everything before forcing(0) is setup (network, reaches, initial states), loop "setup".

Each phase is assigned to the loop it serves: forcing(k) and output(k) to loop k, not to
the loop whose interval they fall in. loop_s = forcing + MC + output; mc_other_s = MC - JIT -
PARALLEL (Python around the routing kernel). sim_h = simulated hours of the loop
(max_loop_size from the run label @L<n>, the case, or one loop of run_hours), so s_per_h
compares loops of different sizes. t_end_s = seconds from the first t-route log line to the
loop's output; t_run_s = the same on measure.py's clock (the HH:MM:SS prefix, 1 s resolution,
seconds since the "==>" line), which lines up with <step>.samples.csv.

Runs that died partway keep the loops they finished. Standard library only.
"""
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PREFIX = re.compile(r"^(\d\d):(\d\d):(\d\d) ")
STAMP = re.compile(r"^\d\d:\d\d:\d\d (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) - ")
PHASES = [
    ("forcing", re.compile(r"\]: lateral inflow DataFrame creation complete in ([\d.]+) seconds")),
    ("jit", re.compile(r"\]: JIT Preprocessing time ([\d.]+) seconds")),
    ("parallel", re.compile(r"\]: PARALLEL TIME ([\d.]+) seconds")),
    ("mc", re.compile(r"\]: MC computation complete in ([\d.]+) seconds")),
    ("output", re.compile(r"\]: output complete in ([\d.]+) seconds")),
]
SETUP = re.compile(r"\]: (?P<k>.+?) complete in (?P<s>[\d.]+) seconds")
FIELDS = ["batch", "cycle", "trial", "repeat", "variant", "loop", "sim_h", "forcing_s", "jit_s",
          "parallel_s", "mc_other_s", "mc_s", "output_s", "loop_s", "s_per_h", "t_end_s", "t_run_s", "complete"]


def loop_hours(variant, case):
    """Hours per loop, and total run hours (None when unknown, e.g. forecast cases)."""
    run_h = int(case["run_hours"]) if str(case.get("run_hours") or "").isdigit() else None
    m = re.search(r"@L(\d+)$", variant)
    if m:
        return int(m.group(1)), run_h
    if str(case.get("max_loop_size") or "").isdigit():
        return int(case["max_loop_size"]), run_h
    return run_h, run_h


def parse_log(text):
    """[(loop index or 'setup', {phase: s}, t_end_s)] in log order, plus setup phases."""
    loops, setup = {}, {}
    n_mc = 0            # MC computations finished so far
    n_forcing = 0       # forcing reads so far
    n_output = 0
    t0 = None
    p0 = None           # measure.py prefix of the first line, seconds of day
    ends, run_ends = {}, {}
    for line in text.splitlines():
        pm0 = PREFIX.match(line)
        p = int(pm0[1]) * 3600 + int(pm0[2]) * 60 + int(pm0[3]) if pm0 else None
        if p is not None and p0 is None:
            p0 = p
        m = STAMP.match(line)
        t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f") if m else None
        if t and t0 is None:
            t0 = t
        for name, rx in PHASES:
            pm = rx.search(line)
            if not pm:
                continue
            s = float(pm.group(1))
            if name == "forcing":
                k = n_forcing
                n_forcing += 1
            elif name == "output":
                k = n_output
                n_output += 1
                if t and t0:
                    ends[k] = (t - t0).total_seconds()
                if p is not None:
                    run_ends[k] = (p - p0) % 86400  # past midnight
            else:
                k = n_mc
                if name == "mc":
                    n_mc += 1
            loops.setdefault(k, {})[name] = s
            break
        else:
            sm = SETUP.search(line)
            if sm and n_forcing == 0:
                setup[sm["k"].strip()] = setup.get(sm["k"].strip(), 0.0) + float(sm["s"])
    # a forcing read for a loop that never ran (the run ended) is not a loop
    out = []
    for k in sorted(loops):
        d = loops[k]
        if "mc" not in d and "output" not in d:
            continue
        out.append((k, d, ends.get(k), run_ends.get(k), "mc" in d and "output" in d))
    return out, setup


def rows_for_batch(batch):
    meta = json.loads((batch / "batch.json").read_text())
    case = meta["case"]
    with open(batch / "runs.csv") as f:
        runs = [r for r in csv.DictReader(f) if r["step"] == "troute" and r["warmup"] == "0"]
    rows = []
    for r in runs:
        p = batch / r["log"]
        if not p.exists():
            continue
        L, run_h = loop_hours(r["variant"], case)
        loops, _ = parse_log(p.read_text(errors="replace"))
        for k, d, t_end, t_run, complete in loops:
            sim_h = None
            if L:
                sim_h = L if run_h is None else max(0, min(L, run_h - k * L))
            mc = d.get("mc", 0.0)
            loop_s = d.get("forcing", 0.0) + mc + d.get("output", 0.0)
            rows.append({
                "batch": meta["batch"], "cycle": r["cycle"], "trial": r["trial"], "repeat": r["repeat"],
                "variant": r["variant"], "loop": k, "sim_h": sim_h,
                "forcing_s": d.get("forcing"), "jit_s": d.get("jit"), "parallel_s": d.get("parallel"),
                "mc_other_s": mc - d.get("jit", 0.0) - d.get("parallel", 0.0) if "mc" in d else None,
                "mc_s": d.get("mc"), "output_s": d.get("output"), "loop_s": loop_s,
                "s_per_h": loop_s / sim_h if sim_h else None,
                "t_end_s": t_end, "t_run_s": t_run, "complete": complete,
            })
    return rows


def write_csv(batch, rows):
    with open(batch / "loops.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: (f"{v:.4f}" if isinstance(v, float) else v) for k, v in r.items()})


def main():
    batch = Path(sys.argv[1]).resolve()
    rows = rows_for_batch(batch)
    write_csv(batch, rows)
    print(f"{len(rows)} loops -> {batch / 'loops.csv'}")


if __name__ == "__main__":
    main()
