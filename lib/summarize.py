#!/usr/bin/env python3
"""Summarize a benchmark batch: results/<batch>/runs.csv -> summary.md, summary.csv.

  summarize.py BATCH_DIR

Per step (and for the whole pipeline) and per variant: number of good runs, median wall
time, interquartile range, min/max, CPU use, peak memory. Speedup is baseline / variant.
It is computed from PAIRED runs (same cycle, same trial: run back to back), reported as the
median of the paired ratios with a 95% bootstrap interval. If the interval excludes 1, the
difference is unlikely to be run-to-run noise.

Warm-up runs and input preparation are excluded. Failed runs are counted and listed.
It also reads t-route's own phase timings from the troute logs. Standard library only.
"""
import csv
import json
import random
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

LAMBDA_MEM_MIB = 10240
PHASE_RES = [
    re.compile(r"\]: (?P<k>.+?) complete in (?P<s>[\d.]+) seconds"),
    re.compile(r"\]: (?P<k>JIT Preprocessing time|PARALLEL TIME) (?P<s>[\d.]+) seconds"),
    re.compile(r"\]: (?P<k>.+?) took (?P<s>[\d.]+) seconds"),
    re.compile(r"\]: (?P<k>Total execution time): (?P<s>[\d.]+) secs"),
]


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def quart(xs):
    if len(xs) < 2:
        return (xs[0], xs[0]) if xs else (None, None)
    q = statistics.quantiles(xs, n=4, method="inclusive")
    return q[0], q[2]


def boot_ci(ratios, n=4000, seed=12345):
    if len(ratios) < 3:
        return None, None
    rng = random.Random(seed)
    meds = sorted(statistics.median(rng.choices(ratios, k=len(ratios))) for _ in range(n))
    return meds[int(0.025 * n)], meds[int(0.975 * n) - 1]


def fmt(x, nd=1):
    return "–" if x is None else f"{x:.{nd}f}"


def troute_phases(batch, rows):
    """{variant: {phase: [seconds, ...]}} from the troute logs of good, timed runs."""
    out = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["step"] != "troute":
            continue
        p = batch / r["log"]
        if not p.exists():
            continue
        seen = defaultdict(float)
        for line in p.read_text(errors="replace").splitlines():
            for rx in PHASE_RES:
                m = rx.search(line)
                if m:
                    seen[m["k"].strip()] += float(m["s"])
                    break
        for k, s in seen.items():
            out[r["variant"]][k].append(s)
    return out


def main():
    batch = Path(sys.argv[1]).resolve()
    meta = json.loads((batch / "batch.json").read_text())
    variants, base = meta["variants"], meta["baseline"]
    with open(batch / "runs.csv") as f:
        allrows = list(csv.DictReader(f))
    timed = [r for r in allrows if r["warmup"] == "0" and r["step"] != "prepare"]
    good = [r for r in timed if r["exit_code"] == "0" and r["oom_killed"] != "True"]
    bad = [r for r in timed if r not in good]
    steps = [s for s in ("preprocess", "troute", "postprocess") if any(r["step"] == s for r in timed)]

    # pipeline total = sum of the steps of one (cycle, trial, variant) run when all succeeded
    runs = defaultdict(dict)
    for r in timed:
        runs[(r["cycle"], r["trial"], r["variant"])][r["step"]] = r
    totals = []
    for (cyc, trial, v), d in runs.items():
        if len(d) == len(steps) and all(x in good for x in d.values()):
            totals.append({"cycle": cyc, "trial": trial, "variant": v, "step": "total",
                           "wall_s": sum(float(x["wall_s"]) for x in d.values()),
                           "cpu": sum((fnum(x["cpu_user_s"]) or 0) + (fnum(x["cpu_sys_s"]) or 0) for x in d.values()),
                           "mem": max((fnum(x["mem_peak_mib"]) or 0) for x in d.values())})

    def series(step):
        if step == "total":
            return [dict(t) for t in totals]
        return [{"cycle": r["cycle"], "trial": r["trial"], "variant": r["variant"], "step": step,
                 "wall_s": float(r["wall_s"]),
                 "cpu": (fnum(r["cpu_user_s"]) or 0) + (fnum(r["cpu_sys_s"]) or 0),
                 "mem": fnum(r["mem_peak_mib"]), "temp": fnum(r["temp_mean_c"]),
                 "freq": fnum(r["freq_mean_mhz"])} for r in good if r["step"] == step]

    table, md = [], []
    c = meta["case"]
    md.append(f"# {meta['batch']}\n")
    md.append(f"Case **{c['name']}**: {c['description']} — VPU {c['vpu']}, forecast type {c['forecast_type']}, "
              f"{c['cycles']} cycle(s) from {c['start_time']}, N_CPUS {c['n_cpus']}.  ")
    o = meta["options"]
    md.append(f"Repeats {o['repeats']}, steps `{o['steps']}`, inputs `{o['inputs']}`, cache `{o['cache']}`, "
              f"Lambda limits {'on' if o['lambda'] == '1' else 'off'}. Baseline: **{base}**.  ")
    h = meta["host"]
    md.append(f"Host: {h['cpu'].get('model')} ({h['cpu'].get('physical')}C/{h['cpu'].get('logical')}T), "
              f"{h['mem_gib']} GiB, kernel {h['kernel']}, Docker {h['docker']}, "
              f"power profile {h['power_profile']}, AC {h['on_ac_power']}.")
    if not meta.get("host_check_passed", True):
        md.append("\n> **Warning:** the host check failed (battery or power-saver). Timings are not comparable with other batches.")
    for v in variants:
        s = meta["builds"][v.split("@")[0]]["sources"]
        md.append(f"\n- `{v}`: routing {s['routing']['sha'][:7]} ({s['routing']['ref']}), "
                  f"usgsdf {s['usgsdf']['sha'][:7]} ({s['usgsdf']['ref']}), t-route {s['troute']['sha'][:7]} ({s['troute']['ref']})")

    md.append("\n## Wall time (s)\n")
    md.append("| step | variant | n | median | IQR | min–max | speedup vs " + base + " (95% CI) | CPU s | peak mem MiB (max) |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    for step in steps + ["total"]:
        rows = series(step)
        by_v = defaultdict(list)
        for r in rows:
            by_v[r["variant"]].append(r)
        base_by_key = {(r["cycle"], r["trial"]): r["wall_s"] for r in by_v.get(base, [])}
        for v in variants:
            rs = by_v.get(v, [])
            if not rs:
                md.append(f"| {step} | {v} | 0 | | | | | | |")
                continue
            w = sorted(r["wall_s"] for r in rs)
            med = statistics.median(w)
            q1, q3 = quart(w)
            mems = [r["mem"] for r in rs if r.get("mem")]
            cpu = statistics.median(r["cpu"] for r in rs)
            sp = lo = hi = None
            if v != base:
                ratios = [base_by_key[(r["cycle"], r["trial"])] / r["wall_s"] for r in rs
                          if (r["cycle"], r["trial"]) in base_by_key and r["wall_s"] > 0]
                if ratios:
                    sp = statistics.median(ratios)
                    lo, hi = boot_ci(ratios)
            sp_txt = "baseline" if v == base else (
                "–" if sp is None else f"{sp:.2f}×" + (f" ({lo:.2f}–{hi:.2f})" if lo else " (n<3)")
                + (" **" + ("faster" if lo > 1 else "slower") + "**" if lo and (lo > 1 or hi < 1) else ""))
            mem_txt = "–" if not mems else f"{statistics.median(mems):.0f} ({max(mems):.0f})"
            if mems and max(mems) > LAMBDA_MEM_MIB:
                mem_txt += " ⚠ >10 GB"
            md.append(f"| {step} | {v} | {len(w)} | {med:.1f} | {fmt(q1)}–{fmt(q3)} | {w[0]:.1f}–{w[-1]:.1f} | "
                      f"{sp_txt} | {cpu:.1f} | {mem_txt} |")
            table.append({"step": step, "variant": v, "n": len(w), "median_s": round(med, 3),
                          "q1_s": q1, "q3_s": q3, "min_s": w[0], "max_s": w[-1],
                          "speedup": sp, "speedup_ci_lo": lo, "speedup_ci_hi": hi,
                          "cpu_s_median": round(cpu, 2),
                          "mem_peak_mib_median": statistics.median(mems) if mems else None,
                          "mem_peak_mib_max": max(mems) if mems else None})

    # thermal / clock: did the machine slow down during the batch?
    allm = [r for st in steps for r in series(st)]
    if allm:
        md.append("\n## Host during the runs\n")
        md.append("| variant | mean CPU temp °C | mean clock MHz |")
        md.append("|---|---|---|")
        for v in variants:
            t = [r["temp"] for r in allm if r["variant"] == v and r.get("temp")]
            fq = [r["freq"] for r in allm if r["variant"] == v and r.get("freq")]
            md.append(f"| {v} | {fmt(statistics.fmean(t)) if t else '–'} | {fmt(statistics.fmean(fq), 0) if fq else '–'} |")

    ph = troute_phases(batch, [r for r in good if r["step"] == "troute"])
    if ph:
        keys = sorted({k for v in ph.values() for k in v},
                      key=lambda k: -statistics.median(ph.get(base, {}).get(k, [0]) or [0]))
        md.append("\n## t-route phases (median s, from t-route's own log lines)\n")
        md.append("| phase | " + " | ".join(variants) + " |")
        md.append("|---|" + "---|" * len(variants))
        for k in keys[:15]:
            md.append(f"| {k} | " + " | ".join(
                fmt(statistics.median(ph[v][k]), 2) if ph.get(v, {}).get(k) else "–" for v in variants) + " |")

    if bad:
        md.append(f"\n## Failed runs ({len(bad)})\n")
        for r in bad:
            md.append(f"- {r['cycle']} {r['variant']} r{r['repeat']} {r['step']}: exit {r['exit_code']}"
                      f"{' (OOM-killed)' if r['oom_killed'] == 'True' else ''} — `{r['log']}`")

    text = "\n".join(md) + "\n"
    (batch / "summary.md").write_text(text)
    if table:
        with open(batch / "summary.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(table[0]))
            w.writeheader()
            w.writerows(table)
    print(text)
    print(f"(written: {batch / 'summary.md'})")


if __name__ == "__main__":
    main()
