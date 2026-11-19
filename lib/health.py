#!/usr/bin/env python3
"""Sanity checks on each run's kept data: runs/<cycle>/<variant>/r<n>/health.json.

  (run by ./bench.sh health, and at the end of ./bench.sh run, inside the t-route image:
   needs xarray, pandas, numpy)
  health.py BATCH_DIR

For every run that kept its data (KEEP=first: repeat 1):
  - t-route output (all loop files): NaN / non-finite / negative values of flow, velocity
    and depth (negative flow at DA gages whose USGS data is negative is marked as such); segments with depth > 100 m and > 1000 m (and the worst ones); whether
    nudge is all NaN; output times continuous across loop files (no gaps or overlaps,
    one step) and covering the run (run_hours, when known);
  - the DA dataframe (da/troute_da.feather): gages, time steps, share of observed values,
    gages with no observation at all;
  - the restart (restart/troute_restart.nc): links, NaN, links starting at zero flow.
Each check is ok / warn / fail with a short note; summarize.py shows them in summary.md.
Files are read one at a time, so year-long runs fit in memory.
"""
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

DEPTH_WARN_M = (100.0, 1000.0)


def check(name, status, note, **data):
    return {"check": name, "status": status, "note": note, **data}


def outputs(d, run_hours):
    """Checks on all loop output files, and the output segment ids (None without files)."""
    files = sorted((d / "outputs/troute").glob("troute_output_*.nc"))
    if not files:
        return [check("output files", "fail", "no troute_output_*.nc")], None
    stats = {v: {"nan": 0, "inf": 0, "neg": 0, "n": 0, "min": np.inf, "max": -np.inf}
             for v in ("flow", "velocity", "depth")}
    deep = {}             # feature_id -> max depth, for segments over the lower threshold
    negs = {}             # feature_id -> min flow, for segments with negative flow
    nudge_n = nudge_finite = 0
    times, seg_ids = [], None
    seg_mismatch = 0
    for f in files:
        with xr.open_dataset(f) as ds:
            fid = ds["feature_id"].values
            if seg_ids is None:
                seg_ids = fid
            elif len(fid) != len(seg_ids) or not np.array_equal(fid, seg_ids):
                seg_mismatch += 1
            times.extend(pd.DatetimeIndex(ds["time"].values))
            for v, s in stats.items():
                if v not in ds:
                    continue
                a = ds[v].values
                s["n"] += a.size
                s["nan"] += int(np.isnan(a).sum())
                s["inf"] += int(np.isinf(a).sum())
                fin = a[np.isfinite(a)]
                if fin.size:
                    s["neg"] += int((fin < 0).sum())
                    s["min"] = min(s["min"], float(fin.min()))
                    s["max"] = max(s["max"], float(fin.max()))
                if v == "flow" and s["neg"]:
                    axis = tuple(i for i, dim in enumerate(ds[v].dims) if dim != "feature_id")
                    fmin = np.nanmin(np.where(np.isfinite(a), a, np.nan), axis=axis) if axis else a
                    for i in np.flatnonzero(fmin < 0):
                        negs[int(fid[i])] = min(negs.get(int(fid[i]), 0.0), float(fmin[i]))
                if v == "depth":
                    axis = tuple(i for i, dim in enumerate(ds[v].dims) if dim != "feature_id")
                    dmax = np.nanmax(np.where(np.isfinite(a), a, np.nan), axis=axis) if axis else a
                    for i in np.flatnonzero(dmax > DEPTH_WARN_M[0]):
                        deep[int(fid[i])] = max(deep.get(int(fid[i]), 0.0), float(dmax[i]))
            if "nudge" in ds:
                a = ds["nudge"].values
                nudge_n += a.size
                nudge_finite += int(np.isfinite(a).sum())
    out = [check("output files", "ok", f"{len(files)} file(s), {len(seg_ids)} segments",
                 files=len(files), segments=int(len(seg_ids)))]
    if seg_mismatch:
        out.append(check("segments", "fail", f"{seg_mismatch} file(s) with different feature_ids than the first"))
    for v, s in stats.items():
        if not s["n"]:
            continue
        bad = s["nan"] + s["inf"]
        status = "fail" if bad else ("warn" if s["neg"] else "ok")
        note = (f"min {s['min']:.4g}, max {s['max']:.4g}; NaN {s['nan']}, inf {s['inf']}, "
                f"negative {s['neg']} of {s['n']}")
        extra = {}
        if v == "flow" and negs:
            worst_neg = sorted(negs.items(), key=lambda kv: kv[1])[:5]
            note += (f" ({len(negs)} segments; most negative "
                     + ", ".join(f"{k} ({x:.3g})" for k, x in worst_neg) + ")")
            extra = {"neg_segments": len(negs), "worst_neg": [[k, round(x, 3)] for k, x in worst_neg]}
        out.append(check(v, status, note, **{k: (None if np.isinf(x) else x) for k, x in s.items()}, **extra))
    over = {t: sum(1 for x in deep.values() if x > t) for t in DEPTH_WARN_M}
    worst = sorted(deep.items(), key=lambda kv: -kv[1])[:5]
    out.append(check("depth > 100 m / > 1000 m", "warn" if over[DEPTH_WARN_M[0]] else "ok",
                     f"{over[DEPTH_WARN_M[0]]} / {over[DEPTH_WARN_M[1]]} segments"
                     + (f"; worst {', '.join(f'{k} ({v:.0f} m)' for k, v in worst)}" if worst else ""),
                     over_100=over[DEPTH_WARN_M[0]], over_1000=over[DEPTH_WARN_M[1]],
                     worst=[[k, round(v, 1)] for k, v in worst]))
    if nudge_n:
        out.append(check("nudge", "warn" if nudge_finite == 0 else "ok",
                         "all NaN (t-route writes no nudge values here; DA can still be active)"
                         if nudge_finite == 0 else f"{nudge_finite} of {nudge_n} values set",
                         finite=nudge_finite, n=nudge_n))
    t = pd.DatetimeIndex(sorted(times))
    if len(t) > 1:
        dt = pd.Series(t).diff().dropna()
        step = dt.mode().iloc[0]
        dups = int(t.duplicated().sum())
        gaps = int((dt > step).sum())
        expected = int(pd.Timedelta(hours=run_hours) / step) if run_hours else None
        status = "fail" if dups or gaps else ("warn" if expected and len(t) != expected else "ok")
        out.append(check("output times", status,
                         f"{len(t)} times {t[0]} .. {t[-1]}, step {step}; duplicates {dups}, gaps {gaps}"
                         + (f"; expected {expected}" if expected else ""),
                         n=len(t), first=str(t[0]), last=str(t[-1]), step_s=step.total_seconds(),
                         duplicates=dups, gaps=gaps, expected=expected))
    return out, seg_ids


def da(d):
    f = d / "da/troute_da.feather"
    if not f.exists():
        return [check("DA feather", "warn", "no da/troute_da.feather")]
    df = pd.read_feather(f)
    vals = df.select_dtypes("number")
    n_obs = int(vals.notna().sum().sum())
    per_gage = vals.notna().mean(axis=1)
    none = int((per_gage == 0).sum())
    status = "fail" if n_obs == 0 else ("warn" if none > len(df) * 0.5 else "ok")
    return [check("DA feather", status,
                  f"{len(df)} gages x {vals.shape[1]} times; {n_obs / max(vals.size, 1) * 100:.1f} % observed; "
                  f"median gage coverage {per_gage.median() * 100:.0f} %; {none} gage(s) with no observation",
                  gages=len(df), times=int(vals.shape[1]), observed=n_obs, no_obs_gages=none)]


def explain_negative_flow(d, checks):
    """Negative flow at a DA gage segment whose USGS observations are negative too
    (flow reversal at regulated or tidal sites) is nudging copying the data, not the model."""
    flow = next((c for c in checks if c["check"] == "flow" and c.get("neg_segments")), None)
    f = d / "da/troute_da.feather"
    if not flow or not f.exists():
        return
    obs = pd.read_feather(f)
    obs.index = obs.index.astype(np.int64)
    neg_obs = set(obs.index[(obs.select_dtypes("number") < 0).any(axis=1)])
    worst = [k for k, _ in flow["worst_neg"]]
    explained = [k for k in worst if k in neg_obs]
    flow["neg_obs_gages"] = explained
    if explained:
        flow["note"] += (f"; {len(explained)} of these {len(worst)} are DA gages whose USGS observations "
                         "are negative too (nudging copies them)")


def restart(d, segments):
    """Initial state (restart/troute_restart.nc, CONUS) on the run's own segments."""
    f = d / "restart/troute_restart.nc"
    if not f.exists():
        return [check("restart", "warn", "no restart/troute_restart.nc")]
    with xr.open_dataset(f) as ds:
        links = ds["links"].values.astype(np.int64)
        keep = np.isin(links, segments) if segments is not None else np.ones(len(links), bool)
        q = ds["qlink1"].values[keep] if "qlink1" in ds else np.array([])
        h = ds["hlink"].values[keep] if "hlink" in ds else np.array([])
    n = int(keep.sum())
    missing = len(segments) - n if segments is not None else 0
    nan = int(np.isnan(q).sum() + np.isnan(h).sum())
    zero = int((q == 0).sum())
    status = "fail" if nan or missing else "ok"
    return [check("restart (initial state)", status,
                  f"{n} of the run's segments ({len(links)} CONUS links); missing {missing}; NaN {nan}; "
                  f"{zero} start at zero flow ({zero / max(n, 1) * 100:.1f} %)",
                  links=n, conus_links=int(len(links)), missing=missing, nan=nan, zero_flow=zero)]


def main():
    batch = Path(sys.argv[1]).resolve()
    meta = json.loads((batch / "batch.json").read_text())
    rh = str(meta["case"].get("run_hours") or "")
    run_hours = int(rh) if rh.isdigit() else None
    owner = batch.stat()
    n = 0
    for rdir in sorted(batch.glob("runs/*/*/r*")):
        d = rdir / "data"
        if not (d / "outputs/troute").is_dir():
            continue
        out_checks, segments = outputs(d, run_hours)
        explain_negative_flow(d, out_checks)
        checks = out_checks + da(d) + restart(d, segments)
        out = rdir / "health.json"
        out.write_text(json.dumps(checks, indent=1, default=str))
        try:  # run as root in the container: hand the file to the batch's owner
            os.chown(out, owner.st_uid, owner.st_gid)
        except OSError:
            pass
        worst = max(checks, key=lambda c: ["ok", "warn", "fail"].index(c["status"]))["status"]
        print(f"{rdir.relative_to(batch)}: {worst} ({sum(c['status'] != 'ok' for c in checks)} not ok)")
        n += 1
    if not n:
        print("no runs with kept t-route output (KEEP=none, or t-route not run)")


if __name__ == "__main__":
    main()
