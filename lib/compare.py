#!/usr/bin/env python3
"""Compare each variant's outputs with the baseline's, cycle by cycle.

  (run by ./bench.sh compare, inside the t-route image: needs xarray, pandas, numpy)
  compare.py BATCH_DIR

Uses repeat 1 of every (cycle, variant), which KEEP=first keeps. For each variant
against the baseline it reports:
  - t-route output (outputs/troute/troute_output_*.nc): per variable, whether it is
    bitwise identical, the max absolute and max relative difference, and how many
    segments differ beyond atol + rtol*|baseline|, on the segments and times both have;
  - the DA dataframe (da/troute_da.feather): shape, gages and NaN counts, max difference;
  - the restart (restart/troute_restart.nc): number of links, max difference on common links.
A speedup only counts if the outputs agree, or differ for a reason we can name.
Writes BATCH_DIR/compare.md and prints it.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

ATOL, RTOL = 1e-6, 1e-5


def cmp_arrays(a, b):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    both_nan = np.isnan(a) & np.isnan(b)
    d = np.where(both_nan, 0.0, np.abs(a - b))
    d = np.where(np.isnan(d), np.inf, d)   # NaN on one side only counts as a difference
    rel = d / np.maximum(np.abs(b), 1e-12)
    bad = d > ATOL + RTOL * np.abs(np.nan_to_num(b))
    return {"identical": bool(np.array_equal(a, b, equal_nan=True)), "max_abs": float(d.max()) if d.size else 0.0,
            "max_rel": float(np.nanmax(np.where(np.isinf(rel), np.nan, rel))) if d.size and np.isfinite(rel).any() else 0.0,
            "bad": bad}


def same_or_diff(r):
    return "identical" if r["identical"] else "max abs diff {:.3g}".format(r["max_abs"])


def one_file(d, pattern):
    fs = sorted(d.glob(pattern))
    return fs[0] if fs else None


def compare_troute(bd, vd, lines):
    bf, vf = one_file(bd / "outputs/troute", "troute_output_*.nc"), one_file(vd / "outputs/troute", "troute_output_*.nc")
    if not bf or not vf:
        lines.append(f"  - t-route output: missing ({'baseline' if not bf else 'variant'})")
        return
    b, v = xr.open_dataset(bf), xr.open_dataset(vf)
    # pandas indexes (not .tolist(): datetime64[ns] would become plain integers)
    ib, iv = b.indexes["feature_id"], v.indexes["feature_id"]
    fb, fv = set(ib), set(iv)
    common = ib.intersection(iv)
    tib, tiv = b.indexes["time"], v.indexes["time"]
    tb, tv = set(tib), set(tiv)
    times = tib.intersection(tiv)
    note = ""
    if fb != fv:
        note += f" segments: {len(fb)} vs {len(fv)} ({len(fb - fv)} only in baseline, {len(fv - fb)} only in variant);"
    if tb != tv:
        note += f" times: {len(tb)} vs {len(tv)} ({len(times)} shared);"
    lines.append(f"  - t-route output `{vf.name}`:{note or ' same segments and times'}")
    lines.append("")
    lines.append("    | variable | identical | max abs diff | max rel diff | segments differing |")
    lines.append("    |---|---|---|---|---|")
    for var in [x for x in ("flow", "velocity", "depth", "nudge") if x in b and x in v]:
        a = v[var].sel(feature_id=common, time=times).transpose("feature_id", "time").values
        c = b[var].sel(feature_id=common, time=times).transpose("feature_id", "time").values
        r = cmp_arrays(a, c)
        nseg = int(r["bad"].any(axis=1).sum())
        lines.append(f"    | {var} | {'yes' if r['identical'] else 'no'} | {r['max_abs']:.3g} | {r['max_rel']:.3g} | "
                     f"{nseg} of {len(common)} |")
    lines.append("")


def compare_da(bd, vd, lines):
    bf, vf = bd / "da/troute_da.feather", vd / "da/troute_da.feather"
    if not bf.exists() or not vf.exists():
        lines.append("  - DA dataframe: missing")
        return
    b, v = pd.read_feather(bf), pd.read_feather(vf)
    # the gage id may be the index or a column depending on the writer
    for df in (b, v):
        if df.index.name is None and df.columns.size and not np.issubdtype(df.dtypes.iloc[0], np.floating):
            df.set_index(df.columns[0], inplace=True)
    txt = (f"  - DA dataframe: {b.shape[0]}×{b.shape[1]} vs {v.shape[0]}×{v.shape[1]}; "
           f"non-NaN values {int(b.notna().sum().sum())} vs {int(v.notna().sum().sum())}")
    rows, cols = b.index.intersection(v.index), b.columns.intersection(v.columns)
    if len(rows) and len(cols):
        bv = b.loc[rows, cols].values.astype(float)
        vv = v.loc[rows, cols].values.astype(float)
        both = ~np.isnan(bv) & ~np.isnan(vv)
        only_b = int((~np.isnan(bv) & np.isnan(vv)).sum())
        only_v = int((np.isnan(bv) & ~np.isnan(vv)).sum())
        d = np.abs(bv[both] - vv[both])
        txt += (f"; on {len(rows)} common gages × {len(cols)} common times: "
                f"{int(both.sum())} values in both, max abs diff {d.max() if d.size else 0:.3g}; "
                f"{only_b} only in baseline, {only_v} only in variant")
        if only_b or only_v:
            mism = (np.isnan(bv) != np.isnan(vv))
            gi = np.where(mism.any(axis=1))[0]
            ex = []
            for g in gi[:8]:
                ts = [str(c) for c, m in zip(cols, mism[g]) if m]
                ex.append(f"{rows[g]} ({len(ts)}: {ts[0]}" + (f" … {ts[-1]}" if len(ts) > 1 else "") + ")")
            txt += f"\n    - gages with NaN mismatches ({len(gi)}): " + "; ".join(ex)
    else:
        txt += "; no common gages/times (index or column layout differs)"
    lines.append(txt)


def compare_restart(bd, vd, lines):
    bf, vf = bd / "restart/troute_restart.nc", vd / "restart/troute_restart.nc"
    if not bf.exists() or not vf.exists():
        lines.append("  - restart: missing")
        return
    b, v = xr.open_dataset(bf), xr.open_dataset(vf)
    dim = "links" if "links" in b.dims else list(b.dims)[0]
    lb, lv = set(b[dim].values.tolist()), set(v[dim].values.tolist())
    common = sorted(lb & lv)
    parts = []
    for var in [x for x in ("qlink1", "qlink2", "hlink") if x in b and x in v]:
        r = cmp_arrays(v[var].sel({dim: common}).values, b[var].sel({dim: common}).values)
        parts.append(f"{var} " + same_or_diff(r))
    lines.append(f"  - restart: {len(lb)} vs {len(lv)} links; on {len(common)} common: " + ", ".join(parts))


def main():
    batch = Path(sys.argv[1])
    meta = json.loads((batch / "batch.json").read_text())
    base, variants = meta["baseline"], [v for v in meta["variants"] if v != meta["baseline"]]
    lines = [f"# Output comparison: {meta['batch']}", "",
             f"Baseline **{base}**, repeat 1 of each cycle. Tolerance for 'differing': |d| > {ATOL} + {RTOL}·|baseline|.", ""]
    for cyc in sorted(p.name for p in (batch / "runs").iterdir() if p.is_dir()):
        bd = batch / "runs" / cyc / base / "r1" / "data"
        lines.append(f"## {cyc}")
        lines.append("")
        if not bd.exists():
            lines.append(f"- baseline data not kept (KEEP=first keeps repeat 1); skipped")
            lines.append("")
            continue
        for v in variants:
            vd = batch / "runs" / cyc / v / "r1" / "data"
            lines.append(f"- **{v}** vs {base}:")
            if not vd.exists():
                lines.append("  - no data kept for this variant")
                continue
            for fn in (compare_troute, compare_da, compare_restart):
                try:
                    fn(bd, vd, lines)
                except Exception as e:  # keep going: one broken file should not hide the rest
                    lines.append(f"  - {fn.__name__}: error {type(e).__name__}: {e}")
        lines.append("")
    text = "\n".join(lines) + "\n"
    (batch / "compare.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
