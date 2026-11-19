#!/usr/bin/env bash
# =============================================================================
# bench.sh: benchmark pipeline variants against each other on the same cases.
#
#   ./bench.sh run CASE VARIANT [VARIANT ...]   measured runs -> results/<batch>/
#   ./bench.sh summarize [BATCH]                tables: median, spread, speedup vs baseline
#   ./bench.sh compare [BATCH]                  do the variants' outputs agree with the baseline?
#   ./bench.sh list                             cases, variants (and their builds), batches
#
# CASE is a file in cases/ (e.g. 03w_short, 03w_short_day); VARIANT is a file in
# variants/ built by ./build.sh. The first variant named is the baseline for speedups
# and comparisons. BATCH defaults to the newest folder in results/.
#
# Options (environment variables, like run.sh):
#   REPEATS=3        timed repeats of every (cycle, variant)
#   STEPS="preprocess troute postprocess"   steps to time, in order
#   INPUTS=own       own: every run makes its own inputs with its own preprocess.
#                    <variant>: routing track. That variant's preprocess prepares each
#                    cycle once (untimed), then every run gets a hard-linked copy, so the
#                    t-route step sees identical inputs. Use with STEPS="troute" or
#                    STEPS="troute postprocess"; N_CPUS is applied to the copy.
#   CACHE=off        NWM download cache for preprocess (images that support it):
#                    off  = no cache, like production Lambda (default)
#                    cold = an empty cache per run (first-run behaviour)
#                    warm = shared cache in data/nwm-cache, filled by the warm-up runs
#   WARMUP=1         one untimed run per variant before the timed ones (first cycle;
#                    every cycle when CACHE=warm). 0 = none
#   LAMBDA=0         1 = run containers with AWS Lambda's limits (6 vCPU, 10 GB memory)
#   KEEP=first       data kept per run: first = repeat 1 keeps outputs/da/restart/yaml
#                    (forcing and RouteLink deleted), other repeats keep only logs;
#                    all = everything; none = logs and metrics only
#   ECHO=0           1 = also print container output to the terminal
#   HOST_CHECK=1     0 = run even on battery / power-saver (results get flagged)
#   MIN_FREE_GB=10   stop before a run if the disk has less free space than this
#   TAG=             label added to the batch folder name
#   DOWNLOAD_JOBS=8  parallel NWM downloads (images that support it)
#   USGS_CHUNK=      gages per USGS request (HYDROTOOLS_CHUNK_SIZE; hydrotools default 5).
#                    40 = one request per troute-usgsdf batch: ~10 requests per 03W run, not ~74
#   RUN_HOURS=       FORECAST_TYPE 5 (chained AnA): hours to run (case value)
#   MAX_LOOP_SIZE=   FORECAST_TYPE 5: t-route max_loop_size in hours (preprocess default 24)
#   MAX_LOOPS=       t-route max_loop_size values (forcing files = hours per loop), e.g.
#                    "240 120 24". Each value runs as its own label VARIANT@L<n>. Needs
#                    INPUTS=<variant>; leave postprocess out (it reads a single output file).
#   HYDROTOOLS_USGS_API_KEY  USGS key; read from .env if present (never commit it)
#   Case values (START_TIME, CYCLES, CYCLE_STEP_H, VPU, N_CPUS, ...) can be overridden too.
#
# Examples:
#   ./bench.sh run 03w_short baseline                           # e2e, 1 cycle, 3 repeats
#   REPEATS=5 CACHE=warm ./bench.sh run 03w_short baseline
#   INPUTS=baseline STEPS=troute N_CPUS=4 ./bench.sh run 03w_short baseline
#   REPEATS=1 ./bench.sh run 03w_short_day baseline             # 24 consecutive cycles
#   REPEATS=1 WARMUP=0 ./bench.sh run 03w_medium baseline       # 10-day medium range
#   LAMBDA=1 INPUTS=baseline STEPS=troute MAX_LOOPS="240 24" ./bench.sh run 03w_medium baseline
#   ./bench.sh summarize && ./bench.sh compare
# =============================================================================
set -euo pipefail

BENCH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="${BENCH}/lib"
RESULTS="${BENCH}/results"

log() { printf '%(%H:%M:%S)T %s\n' -1 "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

latest_batch() {
    local b
    b=$(ls -1d "${RESULTS}"/*/ 2>/dev/null | sort | tail -1) || true
    [[ -n "${b}" ]] || die "no batches in results/"
    echo "${b%/}"
}

# vars_from_build <variant>: prints shell assignments for the variant's images
vars_from_build() {
    local f="${BENCH}/builds/$1.json"
    [[ -f "${f}" ]] || die "variant '$1' has no build; run ./build.sh $1"
    python3 - "${f}" <<'PY'
import json, shlex, sys
b = json.load(open(sys.argv[1]))
for k, key in (("PRE", "preprocess"), ("TRT", "troute"), ("POST", "postprocess")):
    print(f"{k}_IMAGE={shlex.quote(b['images'][key]['tag'])}")
    print(f"{k}_ID={shlex.quote(b['images'][key]['id'])}")
PY
}

check_build_current() {  # warn if the image tag in builds/ no longer points at the recorded image
    local v="$1"
    eval "$(vars_from_build "${v}")"
    for k in PRE TRT POST; do
        local img="${k}_IMAGE" id="${k}_ID" now
        now=$(docker image inspect --format '{{.Id}}' "${!img}" 2>/dev/null) || die "${v}: image ${!img} is missing; run ./build.sh ${v}"
        [[ "${now}" == "${!id}" ]] || die "${v}: ${!img} was rebuilt since builds/${v}.json was written; rerun ./build.sh ${v}"
    done
}

cmd_list() {
    echo "Cases (cases/):"
    for f in "${BENCH}"/cases/*.env; do
        printf '  %-16s %s\n' "$(basename "${f}" .env)" "$(grep -m1 '^DESCRIPTION=' "${f}" | cut -d= -f2- | tr -d '"')"
    done
    echo "Variants (variants/ -> builds/):"
    for f in "${BENCH}"/variants/*.env; do
        local v b
        v=$(basename "${f}" .env); b="${BENCH}/builds/${v}.json"
        if [[ -f "${b}" ]]; then
            b=$(python3 -c 'import json,sys; b=json.load(open(sys.argv[1])); s=b["sources"]; print("built " + b["built_utc"] + "  routing " + s["routing"]["sha"][:7] + "  usgsdf " + s["usgsdf"]["sha"][:7] + "  t-route " + s["troute"]["sha"][:7])' "${b}")
        else
            b="(not built: ./build.sh ${v})"
        fi
        printf '  %-16s %s\n' "${v}" "${b}"
    done
    echo "Batches (results/):"
    ls -1 "${RESULTS}" 2>/dev/null | sed 's/^/  /' || true
}

# ---------------------------------------------------------------------------
cmd_run() {
    local CASE="${1:-}"; shift || true
    [[ -n "${CASE}" && $# -ge 1 ]] || die "usage: ./bench.sh run CASE VARIANT [VARIANT ...]"
    local casef="${BENCH}/cases/${CASE}.env"
    [[ -f "${casef}" ]] || die "no case file ${casef}"
    local VARIANTS=("$@")

    # options
    REPEATS="${REPEATS:-3}"; STEPS="${STEPS:-preprocess troute postprocess}"
    INPUTS="${INPUTS:-own}"; CACHE="${CACHE:-off}"; WARMUP="${WARMUP:-1}"
    LAMBDA="${LAMBDA:-0}"; KEEP="${KEEP:-first}"; ECHO="${ECHO:-0}"
    HOST_CHECK="${HOST_CHECK:-1}"; MIN_FREE_GB="${MIN_FREE_GB:-10}"; DOWNLOAD_JOBS="${DOWNLOAD_JOBS:-8}"; USGS_CHUNK="${USGS_CHUNK:-}"
    MAX_LOOPS="${MAX_LOOPS:-}"
    [[ "${CACHE}" =~ ^(off|cold|warm)$ ]] || die "CACHE must be off, cold or warm"
    [[ "${KEEP}" =~ ^(first|all|none)$ ]] || die "KEEP must be first, all or none"
    read -r -a STEP_LIST <<<"${STEPS}"
    for s in "${STEP_LIST[@]}"; do [[ "${s}" =~ ^(preprocess|troute|postprocess)$ ]] || die "unknown step '${s}'"; done
    if [[ "${INPUTS}" != own ]]; then
        [[ " ${STEPS} " != *" preprocess "* ]] || die "INPUTS=${INPUTS} freezes the inputs; leave preprocess out of STEPS"
        [[ -f "${BENCH}/builds/${INPUTS}.json" ]] || die "INPUTS=${INPUTS}: no such built variant"
    else
        [[ "${STEP_LIST[0]}" == preprocess ]] || die "with INPUTS=own the first step must be preprocess (or set INPUTS=<variant>)"
    fi
    # run labels: the variants, or VARIANT@L<n> per MAX_LOOPS value
    local -a LABELS=()
    if [[ -n "${MAX_LOOPS}" ]]; then
        [[ "${INPUTS}" != own ]] || die "MAX_LOOPS edits troute.yaml of frozen inputs; set INPUTS=<variant>"
        [[ " ${STEPS} " != *" postprocess "* ]] || die "MAX_LOOPS: leave postprocess out of STEPS (it expects one output file)"
        for v in "${VARIANTS[@]}"; do
            for n in ${MAX_LOOPS}; do
                [[ "${n}" =~ ^[1-9][0-9]*$ ]] || die "MAX_LOOPS values must be positive integers"
                LABELS+=("${v}@L${n}")
            done
        done
    else
        LABELS=("${VARIANTS[@]}")
    fi

    # case
    # shellcheck disable=SC1090
    source "${casef}"
    [[ "${START_TIME}" =~ ^[0-9]{12}$ ]] || die "START_TIME must be YYYYMMDDHHMM"

    # secrets: .env (gitignored) holds HYDROTOOLS_USGS_API_KEY
    if [[ -f "${BENCH}/.env" ]]; then set -a; source "${BENCH}/.env"; set +a; fi
    [[ -n "${HYDROTOOLS_USGS_API_KEY:-}" ]] || log "WARNING: HYDROTOOLS_USGS_API_KEY not set; USGS may rate-limit repeated preprocess runs"
    export HYDROTOOLS_USGS_API_KEY="${HYDROTOOLS_USGS_API_KEY:-}"

    for v in "${VARIANTS[@]}"; do check_build_current "${v}"; done
    [[ "${INPUTS}" == own ]] || check_build_current "${INPUTS}"

    # host
    local host_ok=1
    python3 "${LIB}/hostinfo.py" --check >/dev/null || host_ok=0
    if [[ "${host_ok}" == 0 && "${HOST_CHECK}" == 1 ]]; then
        die "host check failed (see above). Fix it, or HOST_CHECK=0 to run anyway (results are flagged)"
    fi

    # keep the machine awake for the whole batch
    # (sleep + idle always; also the lid switch where the session is allowed to block it)
    if [[ -z "${TB_INHIBITED:-}" ]] && command -v systemd-inhibit >/dev/null; then
        local what
        for what in idle:sleep:handle-lid-switch idle:sleep; do
            if systemd-inhibit --what="${what}" --why=probe true 2>/dev/null; then
                export TB_INHIBITED="${what}"
                exec systemd-inhibit --what="${what}" --who=troute_bench \
                    --why="benchmark running" "${BASH_SOURCE[0]}" run "${CASE}" "${VARIANTS[@]}"
            fi
        done
    fi

    # batch folder and metadata
    local stamp batch_name BATCH
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    batch_name="${stamp}_${CASE}${TAG:+_${TAG}}"
    BATCH="${RESULTS}/${batch_name}"
    mkdir -p "${BATCH}/runs"
    local CSV="${BATCH}/runs.csv"
    local CHOWN_IMAGE
    eval "$(vars_from_build "${VARIANTS[0]}")"; CHOWN_IMAGE="${PRE_IMAGE}"

    # cycle start times
    # (epoch arithmetic: GNU date reads "00:00 +0 hours" as a +0 UTC offset, shifting an hour)
    local -a CYC_START=()
    local t0_epoch
    t0_epoch=$(date -u -d "${START_TIME:0:8} ${START_TIME:8:2}:${START_TIME:10:2} UTC" +%s)
    for ((c = 0; c < CYCLES; c++)); do
        CYC_START+=("$(date -u -d "@$((t0_epoch + c * CYCLE_STEP_H * 3600))" +%Y%m%d%H%M)")
    done
    [[ "${CYC_START[0]}" == "${START_TIME}" ]] || die "internal: first cycle ${CYC_START[0]} != START_TIME ${START_TIME}"

    export REPEATS STEPS INPUTS CACHE WARMUP LAMBDA KEEP N_CPUS VPU FORECAST_TYPE \
        START_TIME CYCLES CYCLE_STEP_H DESCRIPTION DOWNLOAD_JOBS USGS_CHUNK MAX_LOOPS
    export RUN_HOURS="${RUN_HOURS:-}" MAX_LOOP_SIZE="${MAX_LOOP_SIZE:-}"
    CASE="${CASE}" VARS="${LABELS[*]}" HOST_OK="${host_ok}" BATCH_NAME="${batch_name}" \
    CYCS="${CYC_START[*]}" \
    python3 - "${BENCH}" "${LIB}/hostinfo.py" >"${BATCH}/batch.json" <<'PY'
import json, os, subprocess, sys, datetime
bench, hostinfo = sys.argv[1], sys.argv[2]
e = os.environ
vs = e["VARS"].split()
bs = sorted({v.split("@")[0] for v in vs} | ({e["INPUTS"]} if e["INPUTS"] != "own" else set()))
print(json.dumps({
    "batch": e["BATCH_NAME"],
    "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "case": {"name": e["CASE"], "description": e.get("DESCRIPTION", ""), "vpu": e["VPU"],
             "forecast_type": e["FORECAST_TYPE"], "start_time": e["START_TIME"],
             "cycles": int(e["CYCLES"]), "cycle_step_h": int(e["CYCLE_STEP_H"]), "n_cpus": e["N_CPUS"],
             "cycle_starts": e["CYCS"].split(),
             "run_hours": e.get("RUN_HOURS", ""), "max_loop_size": e.get("MAX_LOOP_SIZE", "")},
    "options": {k.lower(): e[k] for k in ("REPEATS", "STEPS", "INPUTS", "CACHE", "WARMUP", "LAMBDA", "KEEP", "DOWNLOAD_JOBS", "USGS_CHUNK", "MAX_LOOPS")},
    "variants": vs, "baseline": vs[0],
    "builds": {b: json.load(open(f"{bench}/builds/{b}.json")) for b in bs},
    "host": json.loads(subprocess.run([sys.executable, hostinfo], capture_output=True, text=True).stdout),
    "host_check_passed": e["HOST_OK"] == "1",
}, indent=1))
PY

    local -a LIMITS=()
    [[ "${LAMBDA}" == 1 ]] && LIMITS=(--cpus 6 --memory 10g --memory-swap 10g)
    local echo_flag=()
    [[ "${ECHO}" == 1 ]] && echo_flag=(--echo)
    local uidgid; uidgid="$(id -u):$(id -g)"
    local SHARED_CACHE="${BENCH}/data/nwm-cache"

    fix_owner() {  # give files written by root in containers back to the user
        local d
        for d in "$@"; do
            [[ -d "${d}" ]] || continue
            docker run --rm --network none -v "${d}:/d" --entrypoint chown "${CHOWN_IMAGE}" -R "${uidgid}" /d
        done
    }

    check_disk() {
        local free_gb
        free_gb=$(df -Pk "${BATCH}" | awk 'NR==2 {printf "%d", $4/1048576}')
        (( free_gb >= MIN_FREE_GB )) || die "only ${free_gb} GB free (< MIN_FREE_GB=${MIN_FREE_GB}); stopping before the next run. Batch so far: ${BATCH}"
    }

    # step <name> <run dir> <data dir> <cache dir or ''> <start> <metrics json>
    step() {
        local name="$1" rdir="$2" data="$3" cache="$4" start="$5" mjson="$6"
        local cid="${rdir}/.cid" logf="${rdir}/${name}.log"
        local -a cache_args=() cmd=()
        if [[ -n "${cache}" ]]; then
            mkdir -p "${cache}"
            cache_args=(-v "${cache}:/nwm-cache" --env NWM_CACHE_DIR=/nwm-cache)
        fi
        case "${name}" in
            preprocess)
                cmd=(docker run --cidfile "${cid}" "${LIMITS[@]}" --env PYTHONUNBUFFERED=1
                     -v "${data}:/routing-comparison-nwm-nextgen-troute/t-route"
                     --env START_TIME="${start}" --env FORECAST_TYPE="${FORECAST_TYPE}"
                     --env VPU="${VPU}" --env N_CPUS="${N_CPUS}" --env HYDROTOOLS_USGS_API_KEY
                     --env DOWNLOAD_JOBS="${DOWNLOAD_JOBS}" ${USGS_CHUNK:+--env HYDROTOOLS_CHUNK_SIZE="${USGS_CHUNK}"} \
                     ${RUN_HOURS:+--env RUN_HOURS="${RUN_HOURS}"} ${MAX_LOOP_SIZE:+--env MAX_LOOP_SIZE="${MAX_LOOP_SIZE}"} \
                     "${cache_args[@]}" "${PRE_IMAGE}") ;;
            troute)
                cmd=(docker run --cidfile "${cid}" "${LIMITS[@]}" --env PYTHONUNBUFFERED=1
                     -v "${data}:/ngen/ngen/data:rw" -w /ngen/ngen/data
                     --entrypoint python "${TRT_IMAGE}" -m nwm_routing -f ./troute.yaml) ;;
            postprocess)
                cmd=(docker run --cidfile "${cid}" "${LIMITS[@]}" --env PYTHONUNBUFFERED=1
                     -v "${data}:/routing-comparison-nwm-nextgen-troute/t-route" "${POST_IMAGE}") ;;
        esac
        local rc=0
        python3 "${LIB}/measure.py" --json "${mjson}" --log "${logf}" --cidfile "${cid}" \
            "${echo_flag[@]}" -- "${cmd[@]}" || rc=$?
        fix_owner "${data}" ${cache:+"${cache}"}
        return "${rc}"
    }

    prepare_inputs() {  # <cycle label> <start>: frozen inputs for the routing track
        local label="$1" start="$2" d="${BATCH}/inputs/$1"
        [[ -f "${d}/.complete" ]] && return 0
        mkdir -p "${d}/data"
        eval "$(vars_from_build "${INPUTS}")"
        log "prepare ${label}: ${INPUTS} preprocess (untimed for the comparison)"
        local cache=""; [[ "${CACHE}" == warm ]] && cache="${SHARED_CACHE}"
        step preprocess "${d}" "${d}/data" "${cache}" "${start}" "${d}/preprocess.json" \
            || die "preparing inputs for ${label} failed; see ${d}/preprocess.log"
        python3 "${LIB}/record.py" "${CSV}" "${d}/preprocess.json" batch="${batch_name}" case="${CASE}" \
            cycle="${label}" start_time="${start}" trial= repeat=prepare order_pos= variant="${INPUTS}" \
            step=prepare warmup=1 lambda="${LAMBDA}" cache="${CACHE}" inputs="${INPUTS}" n_cpus="${N_CPUS}" \
            log="inputs/${label}/preprocess.log"
        touch "${d}/.complete"
    }

    # run_one <cycle label> <start> <variant> <repeat> <trial> <order pos> <warmup 0|1>
    run_one() {
        local label="$1" start="$2" v="$3" rep="$4" trial="$5" pos="$6" warm="$7"
        local rdir="${BATCH}/runs/${label}/${v}/r${rep}" cache=""
        local data="${rdir}/data"
        check_disk
        mkdir -p "${data}"
        eval "$(vars_from_build "${v%%@*}")"
        case "${CACHE}" in
            cold) cache="${rdir}/nwm-cache" ;;
            warm) cache="${SHARED_CACHE}" ;;
        esac
        if [[ "${INPUTS}" != own ]]; then
            cp -al "${BATCH}/inputs/${label}/data/." "${data}/"
            # private copy of the config (never edit a hard-linked file in place)
            local loop_sed=()
            [[ "${v}" == *@L* ]] && loop_sed=(-e "s/^\(\s*max_loop_size\s*:\s*\).*/\1${v##*@L}/")
            sed -e "s/^\(\s*cpu_pool\s*:\s*\).*/\1${N_CPUS}/" "${loop_sed[@]}" "${data}/troute.yaml" >"${data}/troute.yaml.new"
            mv -f "${data}/troute.yaml.new" "${data}/troute.yaml"
        fi
        local tag="${label} ${v} r${rep}"; [[ "${warm}" == 1 ]] && tag+=" (warm-up)"
        local s ok=1 line="" secs
        for s in "${STEP_LIST[@]}"; do
            local mj="${rdir}/${s}.json" rc=0
            step "${s}" "${rdir}" "${data}" "${cache}" "${start}" "${mj}" || rc=$?
            python3 "${LIB}/record.py" "${CSV}" "${mj}" batch="${batch_name}" case="${CASE}" cycle="${label}" \
                start_time="${start}" trial="${trial}" repeat="${rep}" order_pos="${pos}" variant="${v}" \
                step="${s}" warmup="${warm}" lambda="${LAMBDA}" cache="${CACHE}" inputs="${INPUTS}" \
                n_cpus="${N_CPUS}" log="runs/${label}/${v}/r${rep}/${s}.log"
            secs=$(python3 -c 'import json,sys; print(f"{json.load(open(sys.argv[1]))["wall_s"]:.1f}s")' "${mj}" 2>/dev/null || echo "?")
            line+=" ${s} ${secs}"
            if [[ "${rc}" != 0 ]]; then
                line+=" FAILED(exit ${rc}; ${rdir#"${BATCH}"/}/${s}.log)"; ok=0; break
            fi
        done
        log "${tag}:${line}"
        # tidy up per KEEP
        [[ -n "${cache}" && "${CACHE}" == cold ]] && rm -rf "${cache}"
        if [[ "${KEEP}" == none || "${warm}" == 1 || ( "${KEEP}" == first && "${rep}" != 1 ) ]]; then
            rm -rf "${data}"
        elif [[ "${KEEP}" == first ]]; then
            rm -rf "${data}/channel_forcing" "${data}/domain/RouteLink_CONUS.nc" "${data}/restart/analysis_assim.nc"
        fi
        return 0
    }

    log "batch ${batch_name}"
    log "  case ${CASE}: VPU ${VPU}, forecast type ${FORECAST_TYPE}, ${CYCLES} cycle(s) from ${START_TIME}, N_CPUS ${N_CPUS}"
    log "  variants: ${LABELS[*]} (baseline: ${LABELS[0]}); repeats ${REPEATS}; steps: ${STEPS}"
    log "  inputs ${INPUTS}; cache ${CACHE}; warm-up ${WARMUP}; lambda limits ${LAMBDA}; keep ${KEEP}"
    trap 'echo; log "interrupted; partial results in ${BATCH}"; exit 130' INT TERM

    local nvar=${#LABELS[@]} trial=0 ci rep i
    for ((ci = 0; ci < CYCLES; ci++)); do
        local start="${CYC_START[ci]}" label
        label=$(printf 'c%02d_%s' "${ci}" "${start}")
        [[ "${INPUTS}" != own ]] && prepare_inputs "${label}" "${start}"
        if [[ "${WARMUP}" == 1 ]] && { (( ci == 0 )) || [[ "${CACHE}" == warm ]]; }; then
            for ((i = 0; i < nvar; i++)); do run_one "${label}" "${start}" "${LABELS[i]}" 0 0 "${i}" 1; done
        fi
        for ((rep = 1; rep <= REPEATS; rep++)); do
            trial=$((trial + 1))
            # rotate the variant order every trial so no variant always runs first (or hot)
            for ((i = 0; i < nvar; i++)); do
                local v="${LABELS[(i + trial - 1) % nvar]}"
                run_one "${label}" "${start}" "${v}" "${rep}" "${trial}" "${i}" 0
            done
        done
        [[ "${INPUTS}" != own && "${KEEP}" != all ]] && rm -rf "${BATCH}/inputs/${label}/data"
    done

    log "done: ${BATCH}"
    python3 "${LIB}/summarize.py" "${BATCH}"
}

cmd_summarize() {
    local b="${1:-$(latest_batch)}"
    [[ -d "${b}" ]] || b="${RESULTS}/${b}"
    [[ -d "${b}" ]] || die "no such batch: ${1}"
    b="$(cd "${b}" && pwd)"   # absolute: docker -v needs it
    python3 "${LIB}/summarize.py" "${b}"
}

cmd_compare() {
    local b="${1:-$(latest_batch)}"
    [[ -d "${b}" ]] || b="${RESULTS}/${b}"
    [[ -d "${b}" ]] || die "no such batch: ${1}"
    b="$(cd "${b}" && pwd)"   # absolute: docker -v needs it
    local base
    base=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["baseline"])' "${b}/batch.json")
    eval "$(vars_from_build "${base}")"
    # runs inside the t-route image, which has xarray/netCDF4/pandas
    docker run --rm --network none -v "${b}:/batch" -v "${LIB}:/tblib:ro" --entrypoint python \
        "${TRT_IMAGE}" /tblib/compare.py /batch
}

case "${1:-}" in
    run) shift; cmd_run "$@" ;;
    summarize) shift; cmd_summarize "$@" ;;
    compare) shift; cmd_compare "$@" ;;
    list) cmd_list ;;
    *) sed -n '3,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
