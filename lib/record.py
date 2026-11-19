#!/usr/bin/env python3
"""Append one measured step to a batch's runs.csv.

  record.py CSV METRICS_JSON key=value ...

The key=value pairs (batch, case, cycle, variant, step, ...) come first in the row,
then the metrics measure.py wrote. The header is written when the file is new.
"""
import csv
import json
import os
import sys

FIELDS = ["batch", "case", "cycle", "start_time", "trial", "repeat", "order_pos", "variant", "step",
          "warmup", "lambda", "cache", "inputs", "n_cpus",
          "wall_s", "container_s", "exit_code", "oom_killed", "cpu_user_s", "cpu_sys_s",
          "mem_peak_mib", "mem_current_max_mib", "freq_mean_mhz", "freq_min_mhz", "temp_mean_c",
          "temp_max_c", "t_start_utc", "log",
          "cores_mean", "outside_cores_mean", "outside_cores_max", "steal_mean_pct", "steal_max_pct",
          "load1_max", "mem_anon_max_mib"]


def main():
    path, metrics = sys.argv[1], sys.argv[2]
    row = dict(kv.split("=", 1) for kv in sys.argv[3:])
    try:
        with open(metrics) as f:
            m = json.load(f)
    except (OSError, ValueError):
        m = {"exit_code": "no-metrics"}
    row.update({k: v for k, v in m.items() if k in FIELDS})
    new = not os.path.exists(path)
    fields = FIELDS
    if not new:  # keep an existing file's columns (batches started before new fields)
        with open(path, newline="") as f:
            fields = next(csv.reader(f), FIELDS)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


if __name__ == "__main__":
    main()
