#!/usr/bin/env python3
"""Plot resource use over time: <step>.samples.csv of each run -> resources.png.

  plot_resources.py BATCH_DIR

One row per (variant, step) for the variant's first timed run: process memory (anon) and
memory incl. page cache (left axis), container CPU cores in use and host cores busy outside the container (right axis),
steal shaded. On t-route rows, dotted lines mark the end of each loop (loops.csv t_run_s).
Shows whether memory grows over a long run, how many cores each phase really uses, and
whether something else on the host competed. Needs matplotlib; summarize.py calls it when
matplotlib is available.
"""
import csv
import sys
from pathlib import Path


def read_csv(p):
    try:
        with open(p, newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def plot(batch):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    runs = [r for r in read_csv(batch / "runs.csv") if r["warmup"] == "0" and r["step"] != "prepare"]
    loops = read_csv(batch / "loops.csv")
    first, panels = {}, []
    for r in runs:
        key = (r["variant"], r["step"])
        if key in first:
            continue
        samples = read_csv(batch / r["log"].replace(".log", ".samples.csv"))
        if len(samples) < 2:
            continue
        first[key] = r
        panels.append((r, samples))
    if not panels:
        return None
    fig, axes = plt.subplots(len(panels), 1, figsize=(10, 2.8 * len(panels)), squeeze=False)
    twins = []
    for ax, (r, s) in zip(axes[:, 0], panels):
        t = [float(x["t_s"]) for x in s]
        mem = [float(x["mem_mib"]) / 1024 for x in s]
        anon = [float(x["anon_mib"]) / 1024 for x in s if x.get("anon_mib")]
        if len(anon) == len(t):
            ax.plot(t, mem, color="#4C78A8", lw=1, ls="--", alpha=0.6, label="memory incl. page cache (GiB)")
            ax.plot(t, anon, color="#4C78A8", lw=1.8, label="process memory, anon (GiB)")
        else:  # samples from before anon was recorded
            ax.plot(t, mem, color="#4C78A8", lw=1.5, label="container memory incl. page cache (GiB)")
        ax.set_ylim(0, max(mem) * 1.15 or 1)
        ax.set_ylabel("memory GiB", color="#4C78A8")
        ax.set_xlabel("seconds since step start")
        ax2 = ax.twinx()
        twins.append(ax2)
        cores = [float(x["cores"]) for x in s]
        outside = [float(x["outside_cores"]) for x in s]
        ax2.plot(t, cores, color="#54A24B", lw=1, label="container cores")
        ax2.plot(t, outside, color="#E45756", lw=1, ls="--", label="outside cores (host, not this run)")
        steal = [float(x.get("steal_cores") or 0) for x in s]
        if any(v > 0.05 for v in steal):
            ax2.fill_between(t, 0, steal, color="#E45756", alpha=0.15, label="steal (cores)")
        ax2.set_ylim(0, max(max(cores), max(outside), 1) * 1.2)
        ax2.set_ylabel("cores")
        if r["step"] == "troute":
            for lp in loops:
                if (lp["variant"], lp["cycle"], lp["trial"]) == (r["variant"], r["cycle"], r["trial"]) \
                        and lp.get("t_run_s"):
                    ax.axvline(float(lp["t_run_s"]), color="#888", lw=0.6, ls=":")
        peak = r.get("mem_peak_mib")
        ax.set_title(f"{r['variant']} {r['step']}  ({r['cycle']}, trial {r['trial']}; "
                     f"{float(r['wall_s']):.0f} s, peak {float(peak) / 1024:.2f} GiB)" if peak else
                     f"{r['variant']} {r['step']}", loc="left", fontsize=10)
    h1, l1 = axes[0, 0].get_legend_handles_labels()
    h2, l2 = twins[0].get_legend_handles_labels()
    fig.suptitle(batch.name, fontsize=9, color="#555")
    fig.legend(h1 + h2, l1 + l2, loc="upper center", bbox_to_anchor=(0.5, 0.97), ncol=4, frameon=False, fontsize=8)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    out = batch / "resources.png"
    fig.savefig(out, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return out


def main():
    out = plot(Path(sys.argv[1]).resolve())
    print(f"-> {out}" if out else "no samples (batches before samples.csv existed have none)")


if __name__ == "__main__":
    main()
