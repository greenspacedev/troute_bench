# troute_bench

Performance benchmarks for the NWM → t-route → NextGen pipeline
(routing-comparison-nwm-nextgen-troute + troute-usgsdf + t-route).

## Quick start

```bash
cp .env.example .env                 # HYDROTOOLS_USGS_API_KEY
./build.sh                           # all variants; first t-route compile ~10+ min
./bench.sh list                      # cases, variants, commits, past batches
powerprofilesctl set performance     # laptop only; plug in
./bench.sh run 03w_short baseline current       # 1 cycle, warm-up + 3 repeats per variant
./bench.sh compare                               # output agreement with baseline
```

VM setup and users: [`vm/README.md`](vm/README.md).

Output: `results/<UTC time>_<case>/`

| File | Contents |
|---|---|
| `summary.md` / `summary.csv` | median wall time per step/variant, spread, speedup vs baseline (95% CI), CPU time, peak memory, t-route phases |
| `compare.md` | agreement with baseline: t-route flows, DA dataframe, restart |
| `runs.csv` | one row per step: wall, CPU, peak memory, OOM, clock, temperature |
| `batch.json` | case, options, commits, image IDs, host |
| `runs/<cycle>/<variant>/r<k>/` | logs and metrics; repeat 1 keeps outputs |

## Variants

`variants/*.env`: source and ref for each of the three repos. `build.sh` resolves refs to SHAs,
builds images, writes `builds/<variant>.json`. `bench.sh` runs those images.

| Variant | routing-comparison | troute-usgsdf | t-route |
|---|---|---|---|
| `baseline` | alk05 `updates_for_da` `10e6b01` | alk05 `temp_routelinkdev` `0a77846` | alk05 `update_for_feather_da` `ca72603` |
| `current` | local `use-our-troute-usgsdf` | local `vectorize-routelink-gages` | greenspacedev `update_for_feather_da` (= baseline) |

- `preprocess.Dockerfile` clones troute-usgsdf unpinned (`git clone --depth 1 -b ...`).
  `lib/pin_usgsdf.py` replaces that `RUN` with a `COPY` at a fixed SHA, in both variants. No
  other Dockerfile changes.
- Local sources build committed work only.
- Image tags are source SHAs (`tb-preprocess:<routing>-<usgsdf>`, `tb-troute:<t-route>`,
  `tb-postprocess:<routing>`); variants sharing a commit share an image.

New variant: copy `variants/current.env` to `variants/<name>.env`, set refs, then
`./build.sh <name>` and `./bench.sh run <case> baseline <name>`. One variant per change to
isolate its effect.

## Tracks

| Track | Set | Measures |
|---|---|---|
| End to end (default) | | preprocess → troute → postprocess, each variant's own code |
| Routing only | `INPUTS=baseline STEPS=troute` | t-route alone; one untimed preprocess per cycle, hard-linked inputs per run. For `N_CPUS` sweeps |
| Preprocess only | `STEPS=preprocess` | download, restart, USGS fetch; combine with `CACHE=` |

Preprocess changes can shift t-route time (e.g. VPU-only restart cuts load time). End to end
counts this; routing only does not.

## Options

Environment variables (`VAR=value ./bench.sh run ...`). Full list: header of `bench.sh`.

| Option | Default | |
|---|---|---|
| `REPEATS` | 3 | timed repeats per (cycle, variant) |
| `STEPS` | `preprocess troute postprocess` | |
| `INPUTS` | `own` | `<variant>`: routing-only on that variant's inputs |
| `CACHE` | `off` | `off`: no NWM cache (Lambda); `cold`: empty per run; `warm`: shared, pre-filled |
| `WARMUP` | 1 | 1: one untimed run per variant, first cycle |
| `LAMBDA` | 0 | 1: 6 vCPU / 10 GB limits per container |
| `KEEP` | `first` | outputs kept: `first`, `all`, `none` |
| `HOST_CHECK` | 1 | 0: skip power check (batch flagged) |
| `N_CPUS`, `START_TIME`, `CYCLES`, ... | case | override case values |

## Cases

| Case | Run | NWM download per variant run |
|---|---|---|
| `03w_short` | 1 short-range cycle, VPU 03W (135,375 segments, 18 h, 216 steps) | ~270 MB |
| `03w_short_day` | 24 hourly cycles, consecutive | ~6.5 GB per pass |

3-day run: `CYCLES=72 REPEATS=1 ./bench.sh run 03w_short_day baseline current`

## Interpreting results

- **Pairing.** Variant order rotates per trial. Speedup = median of paired ratios (same trial).
  Marked **faster**/**slower** when the 95% CI excludes 1. No CI below 3 pairs.
- **Thermals (laptop).** Ryzen 7640U throttles; mean clock and temperature reported per variant.
  Rerun if they differ. Empty on the VM.
- **Network.** Preprocess time includes the GCS download (~270 MB) and USGS queries. `CACHE=warm`
  removes the download.
- **Memory.** cgroup `memory.peak`, including page cache (as Lambda bills). >10 GB flagged.
- **Host check.** Refuses to start on battery or power-saver.

## Known limits (2026-11-13)

- **Medium range (2/3) fails.** `fetch_nwm_data.py` passes `meminput=0` to nwmurl
  (`ValueError: meminput`). Files: `medium_range_mem1/…channel_rt_1.fNNN`, hourly to f240.
  `rename_troute_inputs.py` matches `short_range` names only.
- **Long range (4) fails.** Requests `long_range/…channel_rt.tmNNN` hourly (404). Files:
  `long_range_mem1/…channel_rt_1.fNNN`, 6-hourly.
- **Single loop.** `max_loop_size = nts`: whole run in memory. Fine for 18 h; 10–30 day runs on
  large VPUs need loops.
- **Stock t-route image fails to import.** `requirements.txt` pins `numpy~=1.0`, not `pyarrow`;
  pyarrow 26.0.1 needs NumPy 2 (`ImportError: pyarrow requires NumPy 2.0 or newer`). Variants set
  `TROUTE_PIP_PINS="pyarrow==23.0.0"`; `build.sh` checks the import.
- **03W mask only** (`data/vpu03w_ids.txt` in the image). Other VPUs fail in preprocess.

Longest supported run: one 18-h short-range cycle. Longer = consecutive cycles
(`03w_short_day`, `CYCLES=`).

## Roadmap

1. Baseline vs current on `03w_short`, `03w_short_day`; routing-only `N_CPUS` sweep.
2. Mississippi VPU masks (05, 06, 07, 08, 10L, 10U, 11), mounted into preprocess (baseline image
   unchanged); per-VPU cases.
3. Longer runs: fix medium range, then a hindcast mode (hourly analysis_assim chained over
   days–months, 24-h loops). ~10 GB NWM data per simulated month (14.3 MB/h CONUS).

## Files

| File | |
|---|---|
| `build.sh` | refs → SHAs, export sources, build/tag images, write `builds/<variant>.json` |
| `bench.sh` | `run` / `summarize` / `compare` / `list` |
| `variants/*.env` | one variant each |
| `cases/*.env` | one case each |
| `lib/measure.py` | run one container; wall/CPU/peak memory (cgroup), OOM, clock, temperature |
| `lib/record.py` | append a step to `runs.csv` |
| `lib/summarize.py` | `summary.md` / `summary.csv` |
| `lib/compare.py` | `compare.md` (runs in the t-route image) |
| `lib/hostinfo.py` | host description, power check |
| `lib/pin_usgsdf.py` | pin troute-usgsdf in `preprocess.Dockerfile` |
| `vm/` | VM setup guide, `add-user` |

Gitignored: `cache/` (git mirrors, source exports), `builds/`, `logs/`, `results/`, `data/`
(NWM cache), `.env`.
