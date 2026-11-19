#!/usr/bin/env python3
"""Plot per-loop t-route timings: results/<batch>/loops.csv -> loops.png.

  plot_loops.py BATCH_DIR

One panel per variant (its first complete run): stacked bars of each loop's phases
(forcing, JIT, PARALLEL, MC other, output) and, on the right axis, seconds per simulated
hour. Shows whether loops get slower over a long run and which phase drives it.
Needs matplotlib (VM: apt install python3-matplotlib). summarize.py calls it when
matplotlib is available.
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

PHASES = [("forcing_s", "forcing (qlat read)", "#4C78A8"), ("jit_s", "JIT preprocessing", "#F58518"),
          ("parallel_s", "PARALLEL (routing)", "#54A24B"), ("mc_other_s", "MC other", "#B279A2"),
          ("output_s", "output write", "#9D755D")]


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def plot(batch):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    with open(batch / "loops.csv") as f:
        rows = [r for r in csv.DictReader(f) if r["complete"] == "True"]
    runs = defaultdict(list)
    for r in rows:
        runs[(r["variant"], r["cycle"], r["trial"])].append(r)
    first = {}
    for key in sorted(runs):
        if len(runs[key]) > 1:
            first.setdefault(key[0], sorted(runs[key], key=lambda r: int(r["loop"])))
    if not first:
        return None
    fig, axes = plt.subplots(len(first), 1, figsize=(10, 3.2 * len(first)), squeeze=False)
    for ax, (v, rs) in zip(axes[:, 0], first.items()):
        x = [int(r["loop"]) for r in rs]
        bottom = [0.0] * len(rs)
        for key, label, color in PHASES:
            h = [fnum(r[key]) for r in rs]
            ax.bar(x, h, bottom=bottom, color=color, label=label, width=0.8 if len(rs) < 60 else 1.0)
            bottom = [b + y for b, y in zip(bottom, h)]
        ax.set_ylabel("seconds per loop")
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
        ax.set_xlabel(f"loop ({rs[1]['sim_h'] or '?'} h each)")
        ax.set_title(f"{v}  ({rs[0]['cycle']}, trial {rs[0]['trial']}, {len(rs)} loops)", loc="left")
        sph = [fnum(r["s_per_h"]) for r in rs]
        if any(sph):
            ax2 = ax.twinx()
            ax2.plot(x, sph, color="black", lw=1, marker="." if len(rs) < 60 else None, label="s per simulated h")
            ax2.set_ylabel("s per simulated hour")
            ax2.set_ylim(0, max(sph) * 1.15)
        ax.set_ylim(0, max(bottom) * 1.15)
    h, lab = axes[0, 0].get_legend_handles_labels()
    fig.suptitle(batch.name, fontsize=9, color="#555")
    fig.legend(h, lab, loc="upper center", bbox_to_anchor=(0.5, 0.96), ncol=len(PHASES), frameon=False, fontsize=8)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    out = batch / "loops.png"
    fig.savefig(out, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return out


def main():
    out = plot(Path(sys.argv[1]).resolve())
    print(f"-> {out}" if out else "no run with more than one loop")


if __name__ == "__main__":
    main()
