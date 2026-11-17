# troute_bench

Performance benchmarks for the NWM → t-route → NextGen pipeline
(routing-comparison-nwm-nextgen-troute + troute-usgsdf + t-route). Each of our
improvements is measured against the RA's original code (the **baseline**), on the same
inputs, the same machine and the same Docker images the pipeline runs in production
(AWS Lambda containers).

Status (2026-11-13): first version. It covers what the pipeline can run today: VPU 03W,
short-range cycles (18 h each), singly or as many consecutive cycles. Longer runs and other
VPUs come next (see Roadmap).

## Quick start

```bash
cd "~/code/BYU Hydroinformatics/troute_bench"
cp .env.example .env                 # put your USGS API key in .env (gitignored)
./build.sh                           # images for every variant in variants/ (first time: t-route compiles, ~10+ min)
./bench.sh list                      # cases, variants and their commits, past batches

# before timing anything: plug in, and use the performance power profile
powerprofilesctl set performance

./bench.sh run 03w_short baseline current       # 1 cycle, warm-up + 3 repeats of each variant
./bench.sh compare                               # do current's outputs match the baseline's?
```

On the benchmark VM, skip `powerprofilesctl`; setup and users are in [`vm/README.md`](vm/README.md).

Results go to `results/<UTC time>_<case>/`:

| File | What |
|---|---|
| `summary.md` / `summary.csv` | median wall time per step and variant, spread, speedup vs baseline with a 95% interval, CPU time, peak memory, t-route's internal phases |
| `compare.md` | output agreement with the baseline (t-route flows, DA dataframe, restart) |
| `runs.csv` | one row per measured step (wall, CPU, peak memory, OOM, clock speed, temperature) |
| `batch.json` | everything needed to rerun it: case, options, exact commits and image IDs, host |
| `runs/<cycle>/<variant>/r<k>/` | logs and metrics per run; repeat 1 also keeps its outputs |

## What gets compared

**Variants** (`variants/*.env`) name a source and a ref for each of the three repos.
`build.sh` resolves refs to commit SHAs, builds the images and records the result in
`builds/<variant>.json`. `bench.sh` runs exactly those images.

| Variant | routing-comparison | troute-usgsdf | t-route |
|---|---|---|---|
| `baseline` | alk05 `updates_for_da` `10e6b01` | alk05 `temp_routelinkdev` `0a77846` | alk05 `update_for_feather_da` `ca72603` |
| `current` | local `use-our-troute-usgsdf` | local `vectorize-routelink-gages` | greenspacedev `update_for_feather_da` (same commit as baseline) |

Notes:
- The baseline's `preprocess.Dockerfile` installs troute-usgsdf with an unpinned
  `git clone --depth 1 -b temp_routelinkdev`. To pin it, `build.sh` swaps that one `RUN` line
  for a `COPY` of the same branch at a fixed SHA (`lib/pin_usgsdf.py`). It does the same for
  the current Dockerfile's clone. Nothing else in either Dockerfile changes.
- Local sources build **committed** work only (pushed or not). Commit, then `./build.sh current`.
- Images are tagged by the SHAs inside them (`tb-preprocess:<routing>-<usgsdf>`,
  `tb-troute:<t-route>`, `tb-postprocess:<routing>`), so variants that share a commit share an
  image.

**Adding an improvement:** copy `variants/current.env` to `variants/<name>.env`, point the
refs at the branch or commit with the change, then `./build.sh <name>` and
`./bench.sh run <case> baseline <name> [current ...]`. Keep one variant per idea when you want
to know what each change is worth on its own.

## Tracks

| Track | How | Measures |
|---|---|---|
| End to end (default) | every run does preprocess → troute → postprocess with its own code | what a user (or Lambda) sees |
| Routing only | `INPUTS=baseline STEPS=troute` | t-route alone. One preprocess per cycle (untimed); every run gets a hard-linked copy of the same inputs. Good for `N_CPUS` sweeps and t-route changes |
| Preprocess only | `STEPS=preprocess` | downloads, restart, USGS fetch. Combine with `CACHE=` |

Preprocess improvements can also change t-route's time. For example, the VPU-only restart
cuts t-route's loading time. The end-to-end track counts that effect; the routing-only track
with shared inputs deliberately does not.

## Options

All set as environment variables (`VAR=value ./bench.sh run ...`); see the header of
`bench.sh` for the full list.

| Option | Default | |
|---|---|---|
| `REPEATS` | 3 | timed repeats of each (cycle, variant) |
| `STEPS` | `preprocess troute postprocess` | |
| `INPUTS` | `own` | `<variant>` = routing-only track on frozen inputs |
| `CACHE` | `off` | `off` = no NWM cache (like Lambda); `cold` = empty cache per run; `warm` = shared cache, pre-filled |
| `WARMUP` | 1 | one untimed run per variant first |
| `LAMBDA` | 0 | 1 = 6 vCPU / 10 GB memory limits on every container |
| `KEEP` | `first` | `first`: repeat 1 keeps outputs; `all`; `none` |
| `N_CPUS`, `START_TIME`, `CYCLES`, ... | from the case | case values can be overridden |

## Cases

| Case | What | NWM download per variant run |
|---|---|---|
| `03w_short` | one short-range cycle, VPU 03W (135,375 segments, 18 h, 216 steps) | ~270 MB |
| `03w_short_day` | the 24 hourly cycles of one day, back to back (a production day for one VPU) | ~6.5 GB per pass |

Example: a 3-day sustained run, one repeat each:
`CYCLES=72 REPEATS=1 ./bench.sh run 03w_short_day baseline current`.

## How to read the numbers

- **Order.** Runs are interleaved (the variant order rotates every trial), and speedups are the
  median of *paired* ratios (baseline and variant from the same trial, run back to back).
  When the 95% interval excludes 1, the summary marks the change **faster** or **slower**.
  With fewer than 3 pairs there is no interval: add repeats.
- **Laptop heat.** The Ryzen 7640U slows down when hot. The summary shows the mean CPU temperature
  and clock speed per variant. If one variant ran much hotter or slower, rerun.
- **Network.** Preprocess downloads ~270 MB from Google Cloud and queries USGS, so its time
  includes your connection. `CACHE=warm` takes the NWM download out; USGS stays in.
- **Memory** is the container cgroup's high-water mark (`memory.peak`). That includes page cache
  from file I/O, which is also how a Lambda function's memory is charged. Values above 10 GB are
  flagged against Lambda's maximum.
- **Host check.** `bench.sh` refuses to start on battery or in the power-saver profile
  (`HOST_CHECK=0` overrides it; the batch is then flagged).

## Limits found while building this (2026-11-13)

- **Medium range (FORECAST_TYPE 2/3) does not run** in either variant. `fetch_nwm_data.py` calls
  nwmurl with `meminput=0`, which raises `ValueError: meminput`. The files exist on GCS under
  `medium_range_mem1/…channel_rt_1.fNNN` (hourly to f240), but `rename_troute_inputs.py` only
  matches `short_range` file names.
- **Long range (4) does not run.** It asks for `long_range/…channel_rt.tmNNN` hourly (404).
  The real files are `long_range_mem1/…channel_rt_1.fNNN`, 6-hourly (f006, f012, …).
- **Single-loop memory.** The pipeline sets `max_loop_size = nts`, so t-route holds the whole
  run in one loop. That is fine for 18 h. For 10–30 day runs on large VPUs it will need loops,
  or it runs out of memory.
- **The stock t-route image doesn't import as of 2026-11.** `requirements.txt` pins
  `numpy~=1.0` but not `pyarrow`, so a fresh build installs pyarrow 26.0.1, which needs NumPy 2:
  `ImportError: pyarrow requires NumPy 2.0 or newer, found 1.26.4`. Both variants set
  `TROUTE_PIP_PINS="pyarrow==23.0.0"` (the fix the working `troutetest` image carries), and
  `build.sh` now checks that t-route imports before a variant counts as built. Worth telling
  the RA: whatever builds her image today hits this.
- **Only VPU 03W has a mask** (`data/vpu03w_ids.txt` in the image). Other VPUs fail in
  preprocess.

So the longest run the pipeline supports today is one 18-h short-range cycle. "Longer" here
means many consecutive cycles (`03w_short_day`, `CYCLES=`).

## Roadmap

1. Baseline vs current on `03w_short` and `03w_short_day`, routing-only `N_CPUS` sweep.
2. Masks for the Mississippi VPUs (05, 06, 07, 08, 10L, 10U, 11), mounted into the preprocess
   container so the baseline image works unchanged; per-VPU cases.
3. Longer simulations: medium range (fix the three items above), then a harness-owned
   *hindcast* mode (hourly analysis_assim files chained over days to months, 24-h loops)
   for multi-month runs. Data: ~10 GB of NWM files per simulated month (CONUS files, 14.3 MB/h).
4. Optional: a variant type that runs a native (non-Docker) build, so the 2022 code of
   `troute_paper/repro/perf_2022` can be compared later on the same cases.

## Files

| File | Does |
|---|---|
| `build.sh` | resolve refs → SHAs, export sources, build and tag images, write `builds/<variant>.json` |
| `bench.sh` | `run` / `summarize` / `compare` / `list` |
| `variants/*.env` | one variant each |
| `cases/*.env` | one case each |
| `lib/measure.py` | runs one container; wall/CPU/peak memory from its cgroup, OOM, CPU clock and temperature |
| `lib/record.py` | appends a step to `runs.csv` |
| `lib/summarize.py` | `summary.md` / `summary.csv` |
| `lib/compare.py` | `compare.md` (runs inside the t-route image) |
| `lib/hostinfo.py` | host description and power check |
| `lib/pin_usgsdf.py` | pins troute-usgsdf in `preprocess.Dockerfile` |
| `vm/` | benchmark VM setup (`README.md`) and `add-user` script |

Generated and gitignored: `cache/` (git mirrors, source exports), `builds/`, `logs/`, `results/`,
`data/` (shared NWM cache), `.env`.
