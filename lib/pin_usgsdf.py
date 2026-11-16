#!/usr/bin/env python3
"""Pin troute-usgsdf in a preprocess.Dockerfile to an exact commit.

Both preprocess.Dockerfile versions install troute-usgsdf by cloning a branch inside
the build (`RUN git clone [--depth 1] -b <branch> <repo> /opt/troute-da ...`), so the
image depends on wherever that branch points at build time. This rewrites that one RUN
instruction into `COPY --from=<name> . /opt/troute-da`, where <name> is a BuildKit
named build context holding the troute-usgsdf source exported at the pinned SHA
(build.sh passes `--build-context <name>=<dir>`). The routing-comparison build context
stays exactly as the repo has it (an extra folder in it would change what
`uv pip install -e .` discovers). Nothing else in the Dockerfile changes: the same
`uv pip install -e .` then runs in /opt/troute-da.

Usage: pin_usgsdf.py IN_DOCKERFILE OUT_DOCKERFILE CONTEXT_NAME
"""
import sys


def logical_lines(text):
    """Yield (start, end) line-index ranges of Dockerfile instructions (backslash continuations)."""
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        j = i
        while lines[j].rstrip("\n").rstrip().endswith("\\") and j + 1 < len(lines):
            j += 1
        yield i, j
        i = j + 1


def main():
    src, dst, subdir = sys.argv[1:4]
    text = open(src).read()
    lines = text.splitlines(keepends=True)
    hits = []
    for a, b in logical_lines(text):
        block = "".join(lines[a:b + 1])
        head = block.lstrip().upper()
        if head.startswith("RUN") and "git clone" in block and "/opt/troute-da" in block:
            hits.append((a, b))
    if len(hits) != 1:
        sys.exit(f"pin_usgsdf: expected exactly one `RUN git clone ... /opt/troute-da` in {src}, found {len(hits)}")
    a, b = hits[0]
    old = "".join(lines[a:b + 1])
    new = (f"# troute_bench: troute-usgsdf pinned; replaces:\n"
           # (trailing backslashes dropped so the comment cannot continue onto COPY)
           + "".join(f"#   {l.rstrip().rstrip(chr(92)).rstrip()}\n" for l in old.splitlines())
           + f"COPY --from={subdir} . /opt/troute-da\n")
    open(dst, "w").write("".join(lines[:a]) + new + "".join(lines[b + 1:]))
    print(f"pinned troute-usgsdf: {' '.join(old.split())[:110]}...")


if __name__ == "__main__":
    main()
