# Benchmark VM

## Host

| | |
|---|---|
| Hypervisor | Proxmox (KVM) |
| OS | Ubuntu 26.04 LTS |
| CPU | 24 vCPUs |
| Memory | 121 GiB |
| Disk | 100 GB system (`/`), 800 GB ext4 LVM volume at `/data` (benchmark data) |

## One-time setup (sudo)

1. Docker Engine with the cgroup v2 / systemd driver (Ubuntu default).
   Plots in `summary.md` (`loops.png`): `sudo apt install python3-matplotlib`. Without it,
   summaries still work and skip the plots.
   <!-- TODO: remaining dependencies/tools -->

2. Group and data directory:
   ```bash
   sudo groupadd bench
   sudo chown root:bench /data
   sudo chmod 2775 /data          # setgid: new files inherit group bench
   ```
   `/data` is mounted from `/etc/fstab` (`LABEL=bench /data ext4 defaults 0 2`).

3. User script:
   ```bash
   sudo install -m 750 vm/add-user /usr/local/sbin/add-user
   ```

## Adding a user

User, on their machine:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_benchvm
```
Send `~/.ssh/id_ed25519_benchvm.pub`.

Admin:
```bash
scp <name>.pub benchvm:/tmp/
ssh benchvm
ssh-keygen -lf /tmp/<name>.pub     # must print a fingerprint
sudo add-user <name> /tmp/<name>.pub
rm /tmp/<name>.pub
```

User's `~/.ssh/config`:
```
Host benchvm
    HostName <vm-hostname>
    User <name>
    IdentityFile ~/.ssh/id_ed25519_benchvm
    IdentitiesOnly yes
```

New users are in `docker` and `bench`, not `sudo`. Membership in `docker` is root-equivalent.

## Disabling password login

After every user has a working key:
```bash
echo 'PasswordAuthentication no' | sudo tee /etc/ssh/sshd_config.d/10-no-passwords.conf
sudo sshd -t && sudo systemctl reload ssh
```
`10-` sorts before `50-cloud-init.conf`; sshd uses the first value. Keep a session open and test
from a second terminal.

## Running benchmarks

### Install (once, admin)

```bash
cd /data
git clone <repo-url> troute_bench && cd troute_bench
cp .env.example .env    # add HYDROTOOLS_USGS_API_KEY
echo 'USGS_CHUNK="${USGS_CHUNK:-40}"' >> .env
chmod 640 .env          # shared key, readable by group bench
./build.sh
```

One shared checkout and one USGS key for all users. Images are shared through Docker; users
don't rebuild.

### Run

```bash
cd /data/troute_bench
./bench.sh list                                        # cases, built variants, past batches
[VAR=value ...] ./bench.sh run <case> <variant> [<variant> ...]
```

Use `tmux` for long batches. Batches survive a dropped SSH connection only inside it.

Examples:
```bash
./bench.sh run 03w_short baseline                      # 1 short-range cycle, warm-up + 3 repeats
REPEATS=1 WARMUP=0 ./bench.sh run 03w_medium baseline  # single 240-h run
REPEATS=3 ./bench.sh run 03w_medium baseline <variant> # warm-up + 3 alternating repeats each
```
Options and cases: [`../README.md`](../README.md).

USGS limit: 1000 requests/h per key (`x-ratelimit-limit`, rolling), shared by all users.

`USGS_CHUNK` defaults to 40 on the VM (`.env`) for all runs: 40 gages per request, ~10
requests per 03W run instead of ~74, so long batches stay under the limit and don't fail on
HTTP 429. Data is identical. Cost: the USGS step runs one request per batch (baseline medium
range: 18 s → 99 s; shorter fetch windows cost less). For production-faithful preprocess timing:
`USGS_CHUNK=5 ./bench.sh run ...`.

### Results

```bash
./bench.sh summarize && ./bench.sh compare             # newest batch
less results/<batch>/summary.md
```

| | |
|---|---|
| `wall s`, `median`, `IQR` | per step and variant over repeats; warm-up excluded |
| `speedup vs baseline (95% CI)` | median of paired ratios, first variant = baseline. **faster**/**slower** only when the CI excludes 1; ≥3 pairs needed |
| `CPU s` | user + sys. CPU s / wall s ≈ cores used |
| `peak mem MiB` | cgroup `memory.peak`; ⚠ above Lambda's 10 GB |
| t-route phases | from t-route's log; routing = `MC computation` |
| `compare.md` | output agreement with the baseline (flows, DA dataframe, restart) |

Check `runs.csv` (`exit_code`, `oom_killed`) before reading the numbers. A failed or rate-limited
run (HTTP 429) invalidates the batch.

### VM notes

- No cpufreq or hwmon in the guest: clock and temperature columns are empty.
- `hostinfo.py --check` passes (no battery or power profile to check). `powerprofilesctl` is not needed.
- Stopping a batch: Ctrl-C, then `docker ps` and `docker stop` any leftover container.

## Verify

```bash
id                                   # groups include docker, bench
stat -c '%A %U:%G' /data             # drwxrwsr-x root:bench
touch /data/t && stat -c '%G' /data/t && rm /data/t    # bench
ssh -o PubkeyAuthentication=no benchvm true            # after disabling: Permission denied (publickey)
```
