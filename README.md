# troute_bench

Performance benchmarks for the NWM → t-route → NextGen pipeline
(routing-comparison-nwm-nextgen-troute + troute-usgsdf + t-route).

## Quick start

```bash
cp .env.example .env                 # HYDROTOOLS_USGS_API_KEY
./build.sh                           # all variants; first t-route compile ~10 min
./bench.sh list                      # cases, variants, commits, past batches
./bench.sh run <case> <variant> [<variant> ...]
```

Setup on the benchmark VM: [`vm/README.md`](vm/README.md).

## Variants

`variants/*.env`: source and ref for each of the three repos. `build.sh` resolves refs to SHAs,
builds images, writes `builds/<variant>.json`. `bench.sh` runs exactly those images.

| Variant | routing-comparison | troute-usgsdf | t-route |
|---|---|---|---|
| `baseline` | greenspacedev `medium-range` `8b67fd4` | alk05 `temp_routelinkdev` `0a77846` | alk05 `update_for_feather_da` `ca72603` |
| `baseline-long` | greenspacedev `long-runs` `68bdd6f` | alk05 `temp_routelinkdev` `0a77846` | greenspacedev `feather-da-multiloop` `8c55a5d` |

- `medium-range` = alk05 `updates_for_da` (`10e6b01`) + medium range enabled: ensemble member 1
  for `FORECAST_TYPE=2` (`fetch_nwm_data.py`), `medium_range` file names in
  `rename_troute_inputs.py`.
- `baseline-long`: only the changes needed for runs longer than one forecast.
  - `long-runs` = `medium-range` + `FORECAST_TYPE=5`: chained AnA over `RUN_HOURS` (tm00 of each
    hourly cycle t0+1 h .. t0+N h, named valid time − 1 h), USGS observations over the whole run
    (`makedf --observed-steps 0`), t-route loops of `MAX_LOOP_SIZE` h (default 24).
    `FORECAST_TYPE=12`: the same over NWM Retrospective v3.0 CHRTOUT (1979-02-01 01:00 ..
    2023-02-01 00:00), restart from the retro file at t0.
  - `feather-da-multiloop` = `ca72603` + fix: with `da_from_feather`, runs of more than one loop
    crashed at loop 2. Each loop now takes the feather columns from its own t0.
- `preprocess.Dockerfile` clones troute-usgsdf unpinned; `lib/pin_usgsdf.py` replaces that `RUN`
  with a `COPY` at a fixed SHA. No other Dockerfile changes.
- The stock t-route image fails to import (pyarrow needs NumPy 2; t-route pins NumPy 1). Variants
  set `TROUTE_PIP_PINS="pyarrow==23.0.0"`; `build.sh` checks the import.
- Local sources build committed work only. Image tags are source SHAs
  (`tb-preprocess:<routing>-<usgsdf>`, `tb-troute:<t-route>`, `tb-postprocess:<routing>`);
  variants sharing a commit share an image.

New variant: copy `variants/baseline.env` to `variants/<name>.env`, set refs, `./build.sh <name>`,
then `./bench.sh run <case> baseline <name>`. The first variant named is the reference for
speedups and `./bench.sh compare`.

## Cases

`cases/*.env`. VPU 03W only (mask `data/vpu03w_ids.txt` in the image).

| Case | Run | Forcing download per run |
|---|---|---|
| `03w_short` | 1 short-range cycle (18 h, 216 steps) | ~270 MB |
| `03w_short_day` | 24 hourly short-range cycles | ~6.5 GB per pass |
| `03w_medium` | 1 medium-range cycle, member 1 (240 h); 00/06/12/18 UTC only | ~3.4 GB |
| `03w_ana_1d` | chained AnA, 24 h (`baseline-long`) | ~340 MB |
| `03w_retro_1d` | chained retro v3.0, 24 h (`baseline-long`) | ~1.2 GB |

Long runs: copy an `_1d` case and set `RUN_HOURS`, `START_TIME`. AnA files are ~14 MB/h, retro
~48 MB/h. AnA before 2021-04-20 (NWM v2.0) does not match the v3.0 RouteLink. Chained forecasts
are not a valid long run (hour 240 is superseded by hour 234 of the next cycle).

## Tracks

| Track | Set | Measures |
|---|---|---|
| End to end (default) | | preprocess → troute → postprocess, each variant's own code |
| Routing only | `INPUTS=<variant> STEPS=troute` | t-route alone on one variant's inputs (one untimed preprocess per cycle, hard-linked per run). For `N_CPUS` and `MAX_LOOPS` sweeps |
| Preprocess only | `STEPS=preprocess` | downloads, restart, USGS fetch; combine with `CACHE=` |

Postprocess reads a single output file: leave it out of `STEPS` for multi-loop runs.

## Options

Environment variables (`VAR=value ./bench.sh run ...`). Full list: header of `bench.sh`.

| Option | Default | |
|---|---|---|
| `REPEATS` | 3 | timed repeats per (cycle, variant) |
| `STEPS` | `preprocess troute postprocess` | |
| `INPUTS` | `own` | `<variant>`: routing only, on that variant's inputs |
| `CACHE` | `off` | `off`: no NWM cache; `cold`: empty per run; `warm`: shared, pre-filled |
| `WARMUP` | 1 | 1: one untimed run per variant, first cycle |
| `LAMBDA` | 0 | 1: 6 vCPU / 10 GB limits per container |
| `KEEP` | `first` | outputs kept: `first` (repeat 1), `all`, `none` |
| `HOST_CHECK` | 1 | 0: skip the power check (batch flagged) |
| `USGS_CHUNK` | | gages per USGS request (`HYDROTOOLS_CHUNK_SIZE`, hydrotools default 5). Long-run cases set 5 |
| `RUN_HOURS`, `MAX_LOOP_SIZE` | case | chained runs: hours to run, hours per t-route loop |
| `MAX_LOOPS` | | t-route `max_loop_size` sweep, e.g. `"240 24"`; each runs as `<variant>@L<n>`. Needs `INPUTS=<variant>` |
| `N_CPUS`, `START_TIME`, `CYCLES`, ... | case | override case values |

USGS requests on long windows scale with data volume (50,000 rows per page, 20 pages per
request), not with `USGS_CHUNK`; large chunks on year-long windows hit the page cap.

## Output

`results/<UTC time>_<case>[_<TAG>]/`

| File | Contents |
|---|---|
| `summary.md` / `summary.csv` | per step and variant: median wall time, spread, speedup vs baseline (95% CI), CPU time, peak memory; host load; t-route phases, loops, health checks |
| `loops.csv` / `loops.png` | multi-loop runs: per-loop t-route phases (forcing, JIT, PARALLEL, MC other, output), s per simulated hour, growth over the run |
| `resources.png` | per step: container memory and cores over time, host cores busy outside the run, steal; t-route loop ends marked |
| `compare.md` | `./bench.sh compare`: agreement with the baseline (t-route flows, DA dataframe, restart) |
| `runs.csv` | one row per step: wall, CPU, peak memory, OOM, clock, temperature, cores, outside load, steal |
| `batch.json` | case, options, commits, image IDs, host |
| `runs/<cycle>/<variant>/r<k>/` | logs, `<step>.json`, `<step>.samples.csv` (~1 s), `health.json`; repeat 1 keeps `data/` |

Plots need matplotlib; without it the summary skips them.

## Interpreting results

- **Failures first.** Check `exit_code`, `oom_killed` in `runs.csv` and the failed-runs list.
  A failed or rate-limited (HTTP 429) run invalidates the batch.
- **Speedup.** Variant order rotates per trial. Speedup = median of paired ratios (same trial).
  **faster**/**slower** only when the 95% CI excludes 1; no CI below 3 pairs.
- **Host noise.** Mean CPU temperature and clock (when the host exposes them), cores busy outside
  the run, and steal per step. ⚠ when steal > 2 % or outside load > 1 core: rerun.
- **CPU.** `CPU s / wall s` ≈ cores used. `cores` in the host table is the same from samples.
- **Memory.** cgroup `memory.peak`, including page cache. > 10 GB flagged (Lambda limit).
- **Loops.** Loop 0 includes one-time JIT compilation. `growth` ≈ 1.00 means later loops cost the
  same as early ones; `slope` is the change in seconds per loop.
- **Health.** `warn` notes known quirks (all-NaN `nudge`; negative flow copied from negative USGS
  observations at DA gages); `fail` means missing values, gaps or overlaps in output times, or a
  bad initial state.
- **Network.** Preprocess time includes NWM downloads and USGS queries. `CACHE=warm` removes the
  NWM download.
- **Host check.** `run` refuses to start on battery or a power-saver profile.

## Files

| File | |
|---|---|
| `build.sh` | refs → SHAs, export sources, build/tag images, write `builds/<variant>.json` |
| `bench.sh` | `run` / `summarize` / `compare` / `health` / `list` |
| `variants/*.env`, `cases/*.env` | one variant / case each |
| `lib/measure.py` | run one container: wall, CPU, peak memory (cgroup), OOM, clock, temperature; ~1 s samples incl. host steal and outside load |
| `lib/record.py` | append a step to `runs.csv` |
| `lib/summarize.py` | `summary.md` / `summary.csv` |
| `lib/loops.py` | per-loop t-route timings from `troute.log` → `loops.csv` |
| `lib/plot_loops.py`, `lib/plot_resources.py` | `loops.png`, `resources.png` (matplotlib) |
| `lib/health.py` | `health.json` per run (t-route image) |
| `lib/compare.py` | `compare.md` (t-route image) |
| `lib/hostinfo.py` | host description, power check |
| `lib/pin_usgsdf.py` | pin troute-usgsdf in `preprocess.Dockerfile` |
| `vm/` | VM setup, `add-user` |

Gitignored: `cache/` (git mirrors, source exports), `builds/`, `logs/`, `results/`, `data/`
(NWM cache), `.env`.
