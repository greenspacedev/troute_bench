#!/usr/bin/env python3
"""Run one `docker run` command and measure it.

  measure.py --json OUT.json --log OUT.log --cidfile CID [--echo] -- docker run --cidfile CID ...

The command must create the container with the same --cidfile and must NOT use --rm:
the container is inspected after it exits (exit code, OOM kill, start/finish times)
and then removed here.

While it runs, every 0.25 s this samples
  - the container's cgroup (cgroup v2, systemd driver:
    /sys/fs/cgroup/system.slice/docker-<id>.scope): memory.peak (kernel high-water
    mark), memory.current, memory.stat anon/file, cpu.stat user/system time.
    memory.peak/current include the page cache of files the step reads and writes
    (reclaimable); anon is the processes' own memory (heap, arrays): mem_anon_max_mib;
  - the host: mean CPU clock over all cores (cpufreq) and the CPU temperature
    (k10temp / coretemp), so heat-related slowdown is visible in the results;
  - the host's /proc/stat and /proc/loadavg: busy cores, steal (a VM's vCPUs waiting for
    the hypervisor) and load, so other work on the machine shows up.

About once a second a row goes to OUT.samples.csv (next to OUT.json): seconds since start,
container memory, container CPU cores in use, host busy cores, host cores busy outside the
container, steal (% of host CPU time, and as cores), 1-min load. OUT.json gets their means/maxima (steal_mean_pct,
outside_cores_mean, cores_mean, ...) for runs.csv.

Output lines are written to the log with a wall-clock prefix. Standard library only.
"""
import argparse
import csv
import datetime as dt
import glob
import json
import os
import statistics
import subprocess
import sys
import threading
import time

CG_ROOTS = ["/sys/fs/cgroup/system.slice/docker-{}.scope", "/sys/fs/cgroup/docker/{}"]


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def temp_sensor():
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if (read(f"{d}/name") or "").strip() in ("k10temp", "coretemp", "zenpower"):
            return f"{d}/temp1_input"
    return None


def cpu_freq_mhz():
    vals = []
    for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
        v = read(p)
        if v:
            vals.append(int(v) / 1000)
    return statistics.fmean(vals) if vals else None


def host_cpu():
    """(total jiffies, busy jiffies, steal jiffies) from the /proc/stat cpu line."""
    st = read("/proc/stat")
    if not st:
        return None
    v = [int(x) for x in st.splitlines()[0].split()[1:]]
    # user nice system idle iowait irq softirq steal guest guest_nice; guest is inside user
    total = sum(v[:8])
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    steal = v[7] if len(v) > 7 else 0
    return total, total - idle - steal, steal


SAMPLE_FIELDS = ["t_s", "mem_mib", "anon_mib", "file_mib", "cores", "host_busy_cores", "outside_cores", "steal_pct", "steal_cores", "load1"]


class Sampler(threading.Thread):
    def __init__(self, cidfile, samples_path=None, period=0.25, row_every=1.0):
        super().__init__(daemon=True)
        self.cidfile, self.period, self.row_every = cidfile, period, row_every
        self.samples_path, self.rows = samples_path, []
        self.ncpu = os.cpu_count() or 1
        self.stop = threading.Event()
        self.cg = None
        self.mem_peak = self.mem_cur_max = self.anon_max = 0
        self.anon_now = self.file_now = 0
        self.cpu_user_us = self.cpu_sys_us = 0
        self.freqs, self.temps = [], []
        self.tsensor = temp_sensor()

    def find_cgroup(self):
        cid = (read(self.cidfile) or "").strip()
        if not cid:
            return None
        for pat in CG_ROOTS:
            p = pat.format(cid)
            if os.path.isdir(p):
                return p
        return None

    def sample_row(self, t, prev):
        """One samples.csv row over the interval since prev = (t, cpu_us, host)."""
        host = host_cpu()
        cpu_us = self.cpu_user_us + self.cpu_sys_us
        row = None
        if prev and host and prev[2]:
            dt_s = t - prev[0]
            dtot = host[0] - prev[2][0]
            if dt_s > 0 and dtot > 0:
                cores = (cpu_us - prev[1]) / 1e6 / dt_s
                busy = (host[1] - prev[2][1]) / dtot * self.ncpu
                row = {"t_s": round(t - self.t0, 2), "mem_mib": round(self.mem_now / 2**20, 1),
                       "anon_mib": round(self.anon_now / 2**20, 1), "file_mib": round(self.file_now / 2**20, 1),
                       "cores": round(cores, 2), "host_busy_cores": round(busy, 2),
                       "outside_cores": round(max(0.0, busy - cores), 2),
                       "steal_pct": round((host[2] - prev[2][2]) / dtot * 100, 2),
                       "steal_cores": round((host[2] - prev[2][2]) / dtot * self.ncpu, 2),
                       "load1": float((read("/proc/loadavg") or "0").split()[0])}
        return row, (t, cpu_us, host)

    def run(self):
        self.t0 = time.time()
        self.mem_now = 0
        prev, next_row = self.sample_row(self.t0, None)[1], self.t0 + self.row_every
        while not self.stop.is_set():
            if self.cg is None:
                self.cg = self.find_cgroup()
            if self.cg:
                peak = read(f"{self.cg}/memory.peak")
                cur = read(f"{self.cg}/memory.current")
                if peak:
                    self.mem_peak = max(self.mem_peak, int(peak))
                if cur:
                    self.mem_now = int(cur)
                    self.mem_cur_max = max(self.mem_cur_max, int(cur))
                mstat = read(f"{self.cg}/memory.stat")
                if mstat:
                    for l in mstat.splitlines():
                        k, _, v = l.partition(" ")
                        if k == "anon":
                            self.anon_now = int(v)
                            self.anon_max = max(self.anon_max, self.anon_now)
                        elif k == "file":
                            self.file_now = int(v)
                stat = read(f"{self.cg}/cpu.stat")
                if stat:
                    kv = dict(l.split() for l in stat.splitlines() if len(l.split()) == 2)
                    self.cpu_user_us = int(kv.get("user_usec", self.cpu_user_us))
                    self.cpu_sys_us = int(kv.get("system_usec", self.cpu_sys_us))
            f = cpu_freq_mhz()
            if f:
                self.freqs.append(f)
            if self.tsensor:
                t = read(self.tsensor)
                if t:
                    self.temps.append(int(t) / 1000)
            now = time.time()
            if now >= next_row:
                row, prev = self.sample_row(now, prev)
                if row and self.cg:  # only while the container exists
                    self.rows.append(row)
                next_row = now + self.row_every
            self.stop.wait(self.period)

    def write_samples(self):
        if not self.samples_path:
            return
        with open(self.samples_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=SAMPLE_FIELDS)
            w.writeheader()
            w.writerows(self.rows)

    def stats(self):
        """Means/maxima of the samples for OUT.json."""
        def col(k):
            return [r[k] for r in self.rows]
        if not self.rows:
            return {}
        return {"cores_mean": round(statistics.fmean(col("cores")), 2),
                "outside_cores_mean": round(statistics.fmean(col("outside_cores")), 2),
                "outside_cores_max": round(max(col("outside_cores")), 2),
                "steal_mean_pct": round(statistics.fmean(col("steal_pct")), 2),
                "steal_max_pct": round(max(col("steal_pct")), 2),
                "load1_max": round(max(col("load1")), 2),
                "n_samples": len(self.rows)}


def docker_time(s):
    # 2026-11-13T20:46:01.123456789Z -> seconds since epoch (ns truncated to us)
    if not s or s.startswith("0001"):
        return None
    s = s.rstrip("Z")
    if "." in s:
        a, b = s.split(".")
        s = f"{a}.{b[:6]}"
        fmt = "%Y-%m-%dT%H:%M:%S.%f"
    else:
        fmt = "%Y-%m-%dT%H:%M:%S"
    return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc).timestamp()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--cidfile", required=True)
    ap.add_argument("--echo", action="store_true", help="also print the output to the terminal")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        ap.error("no command")
    if os.path.exists(a.cidfile):
        os.remove(a.cidfile)

    base = a.json[:-5] if a.json.endswith(".json") else a.json
    sampler = Sampler(a.cidfile, samples_path=base + ".samples.csv")
    sampler.start()
    t0 = time.time()
    # line-buffered: the log is readable while the step runs (Python >= 3.14 buffers 128 KiB otherwise)
    with open(a.log, "a", buffering=1) as logf:
        logf.write(f"{time.strftime('%H:%M:%S')} ==> {' '.join(cmd)}\n")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             errors="replace", bufsize=1)
        for line in p.stdout:
            stamped = f"{time.strftime('%H:%M:%S')} {line}"
            logf.write(stamped)
            if a.echo:
                sys.stdout.write(stamped)
        rc = p.wait()
    wall = time.time() - t0
    sampler.stop.set()
    sampler.join()
    sampler.write_samples()

    info = {}
    cid = (read(a.cidfile) or "").strip()
    if cid:
        r = subprocess.run(["docker", "inspect", "--format", "{{json .State}}", cid],
                           capture_output=True, text=True)
        if r.returncode == 0:
            st = json.loads(r.stdout)
            info = {"exit_code": st.get("ExitCode"), "oom_killed": st.get("OOMKilled"),
                    "started": st.get("StartedAt"), "finished": st.get("FinishedAt")}
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
        os.remove(a.cidfile)

    ts, tf = docker_time(info.get("started")), docker_time(info.get("finished"))
    out = {
        "cmd": cmd,
        "wall_s": round(wall, 3),
        "container_s": round(tf - ts, 3) if ts and tf else None,
        "exit_code": info.get("exit_code", rc),
        "oom_killed": bool(info.get("oom_killed")),
        "cpu_user_s": round(sampler.cpu_user_us / 1e6, 3),
        "cpu_sys_s": round(sampler.cpu_sys_us / 1e6, 3),
        "mem_peak_mib": round(sampler.mem_peak / 2**20, 1) if sampler.mem_peak else None,
        "mem_current_max_mib": round(sampler.mem_cur_max / 2**20, 1) if sampler.mem_cur_max else None,
        "mem_anon_max_mib": round(sampler.anon_max / 2**20, 1) if sampler.anon_max else None,
        "cgroup_found": sampler.cg is not None,
        "freq_mean_mhz": round(statistics.fmean(sampler.freqs), 0) if sampler.freqs else None,
        "freq_min_mhz": round(min(sampler.freqs), 0) if sampler.freqs else None,
        "temp_mean_c": round(statistics.fmean(sampler.temps), 1) if sampler.temps else None,
        "temp_max_c": round(max(sampler.temps), 1) if sampler.temps else None,
        "t_start_utc": dt.datetime.fromtimestamp(t0, dt.timezone.utc).isoformat(timespec="seconds"),
        **sampler.stats(),
    }
    with open(a.json, "w") as f:
        json.dump(out, f, indent=1)
    with open(a.log, "a") as logf:
        logf.write(f"{time.strftime('%H:%M:%S')} <== {out['wall_s']:.1f}s exit {out['exit_code']}"
                   f"{' OOM-KILLED' if out['oom_killed'] else ''}\n")
    sys.exit(out["exit_code"] if isinstance(out["exit_code"], int) else 1)


if __name__ == "__main__":
    main()
