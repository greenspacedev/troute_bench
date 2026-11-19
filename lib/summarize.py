#!/usr/bin/env python3
"""Summarize a benchmark batch: results/<batch>/runs.csv -> summary.md, summary.csv.

  summarize.py BATCH_DIR

Per step (and for the whole pipeline) and per variant: number of good runs, median wall
time, interquartile range, min/max, CPU use, peak memory. Speedup is baseline / variant.
It is computed from PAIRED runs (same cycle, same trial: run back to back), reported as the
median of the paired ratios with a 95% bootstrap interval. If the interval excludes 1, the
difference is unlikely to be run-to-run noise.

Warm-up runs and input preparation are excluded. Failed runs are counted and listed.
It also reads t-route's own phase timings from the troute logs, per-loop timings
(lib/loops.py -> loops.csv) for runs of more than one t-route loop, host load and steal
from measure.py's samples, and the health checks (lib/health.py -> health.json).
Plots (loops.png, resources.png) are drawn when matplotlib is installed. Standard library only.
"""
import csv
import json
import random
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loops as loopmod  # noqa: E402

LAMBDA_MEM_MIB = 10240
PHASE_RES = [
    re.compile(r"\]: (?P<k>.+?) complete in (?P<s>[\d.]+) seconds"),
    re.compile(r"\]: (?P<k>JIT Preprocessing time|PARALLEL TIME) (?P<s>[\d.]+) seconds"),
    re.compile(r"\]: (?P<k>.+?) took (?P<s>[\d.]+) seconds"),
    re.compile(r"\]: (?P<k>Total execution time): (?P<s>[\d.]+) secs"),
    # t-route's TIMING SUMMARY buckets: "Routing computations: 17.23 secs, 61.58 %"
    re.compile(r"\]: (?P<k>Network graph construction|Forcing array construction|Routing computations"
               r"|Output writing): (?P<s>[\d.]+) secs"),
]
SKIP_PHASES = {"process"}  # main_v04 "process complete": same span as Total execution time


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
                    if m["k"].strip() not in SKIP_PHASES:
                        seen[m["k"].strip()] += float(m["s"])
                    break
        for k, s in seen.items():
            out[r["variant"]][k].append(s)
    return out


NOISY_STEAL_PCT = 2.0      # mean steal above this: the hypervisor took CPU from the VM
NOISY_OUTSIDE_CORES = 1.0  # mean host cores busy outside the container above this


def host_section(good, variants, steps):
    """Per variant and step: temperature, clock, cores used, other load on the host, steal."""
    def mean(rs, k):
        xs = [fnum(r.get(k)) for r in rs if fnum(r.get(k)) is not None]
        return statistics.fmean(xs) if xs else None

    def mx(rs, k):
        xs = [fnum(r.get(k)) for r in rs if fnum(r.get(k)) is not None]
        return max(xs) if xs else None

    md = ["\n## Host during the runs\n",
          "Means over runs. Cores = CPU cores the container used on average. Outside = host cores busy "
          "with anything else (from /proc/stat; includes the benchmark's own sampler). Steal = CPU time "
          f"the hypervisor took from this VM. ⚠ when mean steal > {NOISY_STEAL_PCT:g} % or outside > "
          f"{NOISY_OUTSIDE_CORES:g} core: timings may be noisy.\n",
          "| variant | step | temp °C | clock MHz | cores | outside cores mean (max) | steal % mean (max) | |",
          "|---|---|---|---|---|---|---|---|"]
    for v in variants:
        for st in steps:
            rs = [r for r in good if r["variant"] == v and r["step"] == st]
            if not rs:
                continue
            om, sm = mean(rs, "outside_cores_mean"), mean(rs, "steal_mean_pct")
            flag = "⚠" if (sm or 0) > NOISY_STEAL_PCT or (om or 0) > NOISY_OUTSIDE_CORES else ""
            md.append(f"| {v} | {st} | {fmt(mean(rs, 'temp_mean_c'))} | {fmt(mean(rs, 'freq_mean_mhz'), 0)} | "
                      f"{fmt(mean(rs, 'cores_mean'), 2)} | {fmt(om, 2)} ({fmt(mx(rs, 'outside_cores_max'), 2)}) | "
                      f"{fmt(sm, 2)} ({fmt(mx(rs, 'steal_max_pct'), 2)}) | {flag} |")
    return md


STATUS_MARK = {"ok": "ok", "warn": "⚠ warn", "fail": "✗ FAIL"}


def health_section(batch):
    """Table of runs/*/*/r*/health.json (written by ./bench.sh health, lib/health.py)."""
    files = sorted(batch.glob("runs/*/*/r*/health.json"))
    if not files:
        return []
    md = ["\n## Health checks (kept outputs; `./bench.sh health`)\n",
          "| run | check | status | note |", "|---|---|---|---|"]
    counts = defaultdict(int)
    for f in files:
        run = "/".join(f.parent.relative_to(batch / "runs").parts)
        try:
            checks = json.loads(f.read_text())
        except ValueError:
            md.append(f"| {run} | – | ✗ FAIL | unreadable health.json |")
            continue
        for c in checks:
            counts[c["status"]] += 1
            md.append(f"| {run} | {c['check']} | {STATUS_MARK.get(c['status'], c['status'])} | {c['note']} |")
    md.insert(1, f"{counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail.\n")
    return md


def resources_plot(batch):
    """resources.png from the runs' samples.csv (after loops.csv: it marks loop ends)."""
    try:
        import plot_resources
    except ImportError:
        return []
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        return []
    return ["\n## Memory and CPU over time\n", "![memory and CPU over time](resources.png)"] \
        if plot_resources.plot(batch) else []


def slope(xs, ys):
    """Least-squares slope of ys on xs (None with fewer than 3 points)."""
    if len(xs) < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else None


def loop_section(batch, variants):
    """loops.csv and the summary.md lines for runs with more than one t-route loop."""
    rows = loopmod.rows_for_batch(batch)
    loopmod.write_csv(batch, rows)
    by_run = defaultdict(list)
    for r in rows:
        if r["complete"]:
            by_run[(r["variant"], r["cycle"], r["trial"])].append(r)
    if not any(len(v) > 1 for v in by_run.values()):
        return []
    md = ["\n## t-route loops (from loops.csv)\n",
          "Per run: loops, then medians over loops 1.. (loop 0 adds one-time JIT compile). "
          "s/h = loop seconds per simulated hour. Growth = median s/h of the last quarter of "
          "loops / first quarter (1.00 = flat); slope = change in loop s per loop.\n",
          "| variant | run | loops | h/loop | loop 0 s | loop s | s/h | forcing | JIT | PARALLEL | MC other | output | growth | slope s/loop |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for (v, cyc, trial), rs in sorted(by_run.items(), key=lambda kv: (variants.index(kv[0][0])
                                                                       if kv[0][0] in variants else 99, kv[0][1:])):
        if len(rs) < 2:
            continue
        rs = sorted(rs, key=lambda r: r["loop"])
        rest = rs[1:]
        med = lambda k: statistics.median([r[k] for r in rest if r[k] is not None]) \
            if any(r[k] is not None for r in rest) else None
        q = max(1, len(rest) // 4)
        sph = [r["s_per_h"] for r in rest]
        growth = None
        if len(rest) >= 4 and all(x is not None for x in sph):
            growth = statistics.median(sph[-q:]) / statistics.median(sph[:q])
        sl = slope([r["loop"] for r in rest], [r["loop_s"] for r in rest])
        md.append(f"| {v} | {cyc} t{trial} | {len(rs)} | {rs[1]['sim_h'] or '–'} | {fmt(rs[0]['loop_s'], 2)} | "
                  f"{fmt(med('loop_s'), 2)} | {fmt(med('s_per_h'), 3)} | {fmt(med('forcing_s'), 2)} | "
                  f"{fmt(med('jit_s'), 2)} | {fmt(med('parallel_s'), 2)} | {fmt(med('mc_other_s'), 2)} | "
                  f"{fmt(med('output_s'), 2)} | {fmt(growth, 2)} | {fmt(sl, 4)} |")
    try:
        import plot_loops
        if plot_loops.plot(batch):
            md.append("\n![per-loop t-route timings](loops.png)")
    except ImportError:
        md.append("\n(loops.png not drawn: matplotlib not installed)")
    return md


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
                           "mem": max((fnum(x["mem_peak_mib"]) or 0) for x in d.values()),
                           "anon": max((fnum(x.get("mem_anon_max_mib")) or 0) for x in d.values()) or None})

    def series(step):
        if step == "total":
            return [dict(t) for t in totals]
        return [{"cycle": r["cycle"], "trial": r["trial"], "variant": r["variant"], "step": step,
                 "wall_s": float(r["wall_s"]),
                 "cpu": (fnum(r["cpu_user_s"]) or 0) + (fnum(r["cpu_sys_s"]) or 0),
                 "mem": fnum(r["mem_peak_mib"]), "anon": fnum(r.get("mem_anon_max_mib")),
                 "temp": fnum(r["temp_mean_c"]),
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
    md.append("Memory: **process** = anon (heap, arrays) at its highest, what a memory limit has to fit; "
              "**incl. cache** = cgroup memory.peak, which also counts the page cache of files read and "
              "written (reclaimable). ⚠ when process memory (or, without it, the peak) > 10 GB.\n")
    md.append("| step | variant | n | median | IQR | min–max | speedup vs " + base + " (95% CI) | CPU s | process mem MiB (max) | incl. cache MiB (max) |")
    md.append("|---|---|---|---|---|---|---|---|---|---|")
    for step in steps + ["total"]:
        rows = series(step)
        by_v = defaultdict(list)
        for r in rows:
            by_v[r["variant"]].append(r)
        base_by_key = {(r["cycle"], r["trial"]): r["wall_s"] for r in by_v.get(base, [])}
        for v in variants:
            rs = by_v.get(v, [])
            if not rs:
                md.append(f"| {step} | {v} | 0 | | | | | | | |")
                continue
            w = sorted(r["wall_s"] for r in rs)
            med = statistics.median(w)
            q1, q3 = quart(w)
            mems = [r["mem"] for r in rs if r.get("mem")]
            anons = [r["anon"] for r in rs if r.get("anon")]
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
            anon_txt = "–" if not anons else f"{statistics.median(anons):.0f} ({max(anons):.0f})"
            if anons and max(anons) > LAMBDA_MEM_MIB:
                anon_txt += " ⚠ >10 GB"
            elif not anons and mems and max(mems) > LAMBDA_MEM_MIB:
                mem_txt += " ⚠ >10 GB"
            md.append(f"| {step} | {v} | {len(w)} | {med:.1f} | {fmt(q1)}–{fmt(q3)} | {w[0]:.1f}–{w[-1]:.1f} | "
                      f"{sp_txt} | {cpu:.1f} | {anon_txt} | {mem_txt} |")
            table.append({"step": step, "variant": v, "n": len(w), "median_s": round(med, 3),
                          "q1_s": q1, "q3_s": q3, "min_s": w[0], "max_s": w[-1],
                          "speedup": sp, "speedup_ci_lo": lo, "speedup_ci_hi": hi,
                          "cpu_s_median": round(cpu, 2),
                          "mem_peak_mib_median": statistics.median(mems) if mems else None,
                          "mem_peak_mib_max": max(mems) if mems else None,
                          "mem_anon_mib_median": statistics.median(anons) if anons else None,
                          "mem_anon_mib_max": max(anons) if anons else None})

    # thermal / clock / other load: did the machine slow down or get busy during the batch?
    md.extend(host_section(good, variants, steps))

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

    md.extend(loop_section(batch, variants))
    md.extend(resources_plot(batch))
    md.extend(health_section(batch))

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
