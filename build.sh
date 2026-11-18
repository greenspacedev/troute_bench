#!/usr/bin/env bash
# =============================================================================
# build.sh: build the pipeline images for benchmark variants, pinned by commit SHA.
#
#   ./build.sh                 # every variant in variants/*.env
#   ./build.sh baseline        # only the variants you name
#   FORCE=1 ./build.sh baseline # rebuild even if an image for those SHAs exists
#
# For each variant this:
#   1. resolves ROUTING_REF / USGSDF_REF / TROUTE_REF to commit SHAs (fetching from
#      the variant's *_SRC: a git URL or a local clone; committed state only),
#   2. exports each source tree at that SHA (git archive) into cache/ctx/,
#   3. builds three images, tagged by the SHAs they contain:
#        tb-preprocess:<routing12>-<usgsdf12>  (troute-usgsdf pinned, see lib/pin_usgsdf.py)
#        tb-troute:<troute12>
#        tb-postprocess:<routing12>
#      An image that already exists for the same SHAs is reused, so variants that
#      share a commit share an image (and t-route is compiled only once).
#   4. writes builds/<variant>.json: sources, refs, SHAs, image tags and IDs.
#
# bench.sh runs exactly the images recorded in builds/<variant>.json. Rerun build.sh
# after committing new work; a moved branch produces a new image tag.
# =============================================================================
set -euo pipefail

BENCH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE="$(dirname "${BENCH}")"
export CODE
CACHE="${BENCH}/cache"
mkdir -p "${CACHE}/repos" "${CACHE}/ctx" "${BENCH}/builds" "${BENCH}/logs"

log() { printf '%(%H:%M:%S)T %s\n' -1 "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# resolve <component> <src> <ref> -> prints the commit SHA, fetching into cache/repos/<component>.git
resolve() {
    local comp="$1" src="$2" ref="$3" repo="${CACHE}/repos/$1.git" key sha
    [[ -d "${repo}" ]] || git init -q --bare "${repo}"
    key=$(printf '%s' "${src}" | md5sum | cut -c1-10)
    if [[ "${ref}" =~ ^[0-9a-f]{40}$ ]] && git -C "${repo}" cat-file -e "${ref}^{commit}" 2>/dev/null; then
        echo "${ref}"; return
    fi
    if [[ "${ref}" =~ ^[0-9a-f]{7,40}$ ]]; then
        # a SHA: fetch every branch of the source, then look the commit up
        git -C "${repo}" fetch -q "${src}" "+refs/heads/*:refs/bench/${key}/*" >&2 \
            || die "${comp}: fetch from ${src} failed"
        sha=$(git -C "${repo}" rev-parse -q --verify "${ref}^{commit}") \
            || die "${comp}: commit ${ref} not found on any branch of ${src}"
    else
        git -C "${repo}" fetch -q "${src}" "${ref}" >&2 || die "${comp}: ref '${ref}' not found in ${src}"
        sha=$(git -C "${repo}" rev-parse FETCH_HEAD)
        git -C "${repo}" update-ref "refs/bench/${key}/${ref//\//_}" "${sha}"
    fi
    echo "${sha}"
}

# export <component> <sha> -> prints the directory holding the source tree at that SHA
export_tree() {
    local comp="$1" sha="$2" dest="${CACHE}/ctx/$1-${2:0:12}"
    if [[ ! -f "${dest}.complete" ]]; then
        rm -rf "${dest}"; mkdir -p "${dest}"
        git -C "${CACHE}/repos/${comp}.git" archive "${sha}" | tar -x -C "${dest}"
        touch "${dest}.complete"
    fi
    echo "${dest}"
}

have_image() { docker image inspect "$1" >/dev/null 2>&1; }

# build_image <tag> <logfile> <docker build args...>
build_image() {
    local tag="$1" logf="$2"; shift 2
    if have_image "${tag}" && [[ "${FORCE:-0}" != 1 ]]; then
        log "  ${tag}: exists, reusing"; return
    fi
    log "  ${tag}: building (log: ${logf#"${BENCH}"/})"
    if ! docker build --progress=plain -t "${tag}" "$@" >"${logf}" 2>&1; then
        tail -30 "${logf}" >&2; die "docker build of ${tag} failed; full log: ${logf}"
    fi
    log "  ${tag}: built"
}

image_id() { docker image inspect --format '{{.Id}}' "$1"; }

build_variant() {
    local name="$1" envf="${BENCH}/variants/$1.env"
    [[ -f "${envf}" ]] || die "no variant file ${envf}"
    # each variant is read in a clean subshell environment
    (
        set -euo pipefail
        unset DESCRIPTION ROUTING_SRC ROUTING_REF USGSDF_SRC USGSDF_REF TROUTE_SRC TROUTE_REF TROUTE_PIP_PINS
        # shellcheck disable=SC1090
        source "${envf}"
        for v in ROUTING_SRC ROUTING_REF USGSDF_SRC USGSDF_REF TROUTE_SRC TROUTE_REF; do
            [[ -n "${!v:-}" ]] || die "${envf}: ${v} is not set"
        done
        log "== ${name}: ${DESCRIPTION:-}"
        local r u t
        r=$(resolve routing "${ROUTING_SRC}" "${ROUTING_REF}")
        u=$(resolve usgsdf "${USGSDF_SRC}" "${USGSDF_REF}")
        t=$(resolve troute "${TROUTE_SRC}" "${TROUTE_REF}")
        log "  routing-comparison ${ROUTING_REF} -> ${r:0:12}"
        log "  troute-usgsdf      ${USGSDF_REF} -> ${u:0:12}"
        log "  t-route            ${TROUTE_REF} -> ${t:0:12}"

        local rdir udir tdir pre_tag tr_tag post_tag
        rdir=$(export_tree routing "${r}")
        udir=$(export_tree usgsdf "${u}")
        tdir=$(export_tree troute "${t}")
        pre_tag="tb-preprocess:${r:0:12}-${u:0:12}"
        tr_tag="tb-troute:${t:0:12}"
        post_tag="tb-postprocess:${r:0:12}"

        # preprocess: the routing tree as-is is the build context; the pinned usgsdf
        # tree comes in as a named build context (Dockerfile kept outside both)
        local pdf="${CACHE}/ctx/preprocess-${r:0:12}-${u:0:12}.Dockerfile"
        python3 "${BENCH}/lib/pin_usgsdf.py" "${rdir}/preprocess.Dockerfile" "${pdf}" bench_usgsdf >/dev/null
        build_image "${pre_tag}" "${BENCH}/logs/build_${pre_tag//[:\/]/_}.log" \
            -f "${pdf}" --build-context "bench_usgsdf=${udir}" \
            --label "tb.routing=${r}" --label "tb.usgsdf=${u}" "${rdir}"
        build_image "${post_tag}" "${BENCH}/logs/build_${post_tag//[:\/]/_}.log" \
            -f "${rdir}/postprocess.Dockerfile" --label "tb.routing=${r}" "${rdir}"
        build_image "${tr_tag}" "${BENCH}/logs/build_${tr_tag//[:\/]/_}.log" \
            --label "tb.troute=${t}" "${tdir}"
        # Optional build fix: extra pip pins layered on top of the stock t-route image.
        # t-route's requirements.txt leaves pyarrow unpinned while pinning numpy~=1.0;
        # a fresh resolve gets a pyarrow that needs NumPy 2 and t-route fails on import.
        if [[ -n "${TROUTE_PIP_PINS:-}" ]]; then
            local base_tag="${tr_tag}" pin_hash
            pin_hash=$(printf '%s' "${TROUTE_PIP_PINS}" | md5sum | cut -c1-8)
            tr_tag="${base_tag}-pins${pin_hash}"
            # shellcheck disable=SC2086
            build_image "${tr_tag}" "${BENCH}/logs/build_${tr_tag//[:\/]/_}.log" \
                --label "tb.troute=${t}" --label "tb.troute_pip_pins=${TROUTE_PIP_PINS}" - <<EOF
FROM ${base_tag}
RUN uv pip install --python /t-route/.venv/bin/python ${TROUTE_PIP_PINS}
EOF
        fi
        # smoke test: t-route must at least import (catches broken environments at build time)
        docker run --rm --network none --entrypoint python "${tr_tag}" -c "import nwm_routing.__main__" \
            >/dev/null 2>"${BENCH}/logs/import_${tr_tag//[:\/]/_}.log" \
            || { tail -3 "${BENCH}/logs/import_${tr_tag//[:\/]/_}.log" >&2;
                 die "${tr_tag}: t-route fails to import (set TROUTE_PIP_PINS in variants/${name}.env?)"; }
        log "  ${tr_tag}: imports OK"

        NAME="${name}" DESC="${DESCRIPTION:-}" \
        RS="${ROUTING_SRC}" RR="${ROUTING_REF}" RSHA="${r}" \
        US="${USGSDF_SRC}" UR="${USGSDF_REF}" USHA="${u}" \
        TS="${TROUTE_SRC}" TR="${TROUTE_REF}" TSHA="${t}" PINS="${TROUTE_PIP_PINS:-}" \
        PRE="${pre_tag}" PREID="$(image_id "${pre_tag}")" \
        TRT="${tr_tag}" TRID="$(image_id "${tr_tag}")" \
        POST="${post_tag}" POSTID="$(image_id "${post_tag}")" \
        python3 - >"${BENCH}/builds/${name}.json" <<'PY'
import json, os, datetime
e = os.environ
comp = lambda s, r, sha: {"src": s, "ref": r, "sha": sha}
print(json.dumps({
    "variant": e["NAME"], "description": e["DESC"],
    "built_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "sources": {"routing": comp(e["RS"], e["RR"], e["RSHA"]),
                "usgsdf": comp(e["US"], e["UR"], e["USHA"]),
                "troute": comp(e["TS"], e["TR"], e["TSHA"])},
    "images": {"preprocess": {"tag": e["PRE"], "id": e["PREID"]},
               "troute": {"tag": e["TRT"], "id": e["TRID"]},
               "postprocess": {"tag": e["POST"], "id": e["POSTID"]}},
    "troute_pip_pins": e["PINS"],
    "notes": "preprocess.Dockerfile's `git clone` of troute-usgsdf replaced by a COPY of the pinned SHA (lib/pin_usgsdf.py)"
             + ("; t-route image has extra pip pins layered on top: " + e["PINS"] if e["PINS"] else ""),
}, indent=1))
PY
        log "  -> builds/${name}.json"
    )
}

VARIANTS=("$@")
if [[ ${#VARIANTS[@]} -eq 0 ]]; then
    for f in "${BENCH}"/variants/*.env; do VARIANTS+=("$(basename "${f}" .env)"); done
fi
for v in "${VARIANTS[@]}"; do build_variant "${v}"; done
