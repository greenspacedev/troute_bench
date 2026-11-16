#!/usr/bin/env python3
"""Print a JSON description of the benchmark host: CPU, memory, kernel, Docker, power state.

  hostinfo.py            -> JSON on stdout
  hostinfo.py --check    -> also exit 2 if the machine is on battery or not in the
                            'performance'/'balanced' power profile (numbers would be off)
"""
import json
import os
import platform
import subprocess
import sys


def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def read(p):
    try:
        return open(p).read().strip()
    except OSError:
        return None


def main():
    cpu = {}
    for line in (read("/proc/cpuinfo") or "").splitlines():
        if line.startswith("model name"):
            cpu["model"] = line.split(":", 1)[1].strip()
            break
    cpu["logical"] = os.cpu_count()
    cores = set()
    for d in os.listdir("/sys/devices/system/cpu"):
        c = read(f"/sys/devices/system/cpu/{d}/topology/core_id")
        pk = read(f"/sys/devices/system/cpu/{d}/topology/physical_package_id")
        if c is not None:
            cores.add((pk, c))
    cpu["physical"] = len(cores) or None
    cpu["governor"] = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    cpu["epp"] = read("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference")
    mem_kb = next((int(l.split()[1]) for l in (read("/proc/meminfo") or "").splitlines()
                   if l.startswith("MemTotal")), None)
    ac = None
    for d in ("/sys/class/power_supply/ACAD", "/sys/class/power_supply/AC", "/sys/class/power_supply/ADP1"):
        v = read(f"{d}/online")
        if v is not None:
            ac = v == "1"
            break
    info = {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "os": (dict(l.split("=", 1) for l in (read("/etc/os-release") or "").splitlines() if "=" in l)
               .get("PRETTY_NAME", "").strip('"')),
        "cpu": cpu,
        "mem_gib": round(mem_kb / 2**20, 1) if mem_kb else None,
        "docker": sh("docker", "version", "--format", "{{.Server.Version}}"),
        "power_profile": sh("powerprofilesctl", "get"),
        "on_ac_power": ac,
    }
    print(json.dumps(info, indent=1))
    if "--check" in sys.argv:
        problems = []
        if ac is False:
            problems.append("running on battery (plug in the charger)")
        if info["power_profile"] == "power-saver":
            problems.append("power profile is power-saver (powerprofilesctl set performance)")
        if problems:
            print("HOST CHECK FAILED: " + "; ".join(problems), file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
