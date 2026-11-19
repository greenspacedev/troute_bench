# Benchmark VM

## Host

| | |
|---|---|
| Hypervisor | Proxmox (KVM), CPU type `host` |
| OS | Ubuntu 26.04 LTS |
| CPU | AMD EPYC 7532 (Zen 2), 24 vCPUs; AVX2/FMA, no AVX-512 |
| Memory | 121 GiB |
| Disk | 1 TB, LVM: 100 GB `/`, 800 GB `/data` (benchmark data, Docker), rest of the VG free |

- vCPUs are host hyperthreads, not pinned; the guest sees 24 cores × 1 thread. Once other VMs
  share the host, expect SMT contention and steal: check the host table in `summary.md`.
- No cpufreq or hwmon in the guest: clock and temperature columns are empty.
- Proxmox's default CPU type (`kvm64`/QEMU) hides AVX/AVX2; keep `host`. Check:
  `grep -o avx2 /proc/cpuinfo | head -1`.

## Setup (once, sudo)

### 1. Storage

```bash
lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT
sudo vgs                                  # VFree: space for /data
df -h /
```

Keep `/` for the OS. Benchmark data, results and Docker images go on `/data`.

### 2. Data volume

```bash
sudo lvcreate -n bench -L 800G ubuntu-vg
sudo mkfs.ext4 -L bench /dev/ubuntu-vg/bench
sudo mkdir -p /data
echo 'LABEL=bench /data ext4 defaults 0 2' | sudo tee -a /etc/fstab
sudo mount -a && df -h /data
```

### 3. Packages and Docker on /data

```bash
sudo apt install docker.io docker-buildx git tmux python3-matplotlib
```

`python3-matplotlib`: plots in `summary.md`; summaries work without it.

Docker 29 keeps images in containerd, so both roots move:
```bash
sudo systemctl stop docker.socket docker containerd
echo '{ "data-root": "/data/docker" }' | sudo tee /etc/docker/daemon.json
sudo mkdir -p /etc/containerd
printf 'version = 3\nroot = "/data/containerd"\n' | sudo tee /etc/containerd/config.toml
sudo systemctl start containerd docker
docker info -f '{{.DockerRootDir}}'       # /data/docker
```

On an install that already has images, copy them before starting again:
```bash
sudo rsync -aHAXS --numeric-ids /var/lib/docker /var/lib/containerd /data/
sudo mv /var/lib/docker /var/lib/docker.old && sudo mv /var/lib/containerd /var/lib/containerd.old
# start, check `docker images`, then: sudo rm -rf /var/lib/docker.old /var/lib/containerd.old
```

### 4. Group and permissions

```bash
sudo groupadd bench
sudo chown root:bench /data
sudo chmod 2775 /data                     # setgid: new files inherit group bench
sudo install -m 750 vm/add-user /usr/local/sbin/add-user
```

Users are added to `docker` and `bench` (see [Adding a user](#adding-a-user)).

### 5. Clone

```bash
cd /data
git clone https://github.com/greenspacedev/troute_bench.git && cd troute_bench
cp .env.example .env                      # add HYDROTOOLS_USGS_API_KEY
echo 'USGS_CHUNK="${USGS_CHUNK:-40}"' >> .env
chmod 640 .env                            # shared key, readable by group bench
```

One shared checkout and one USGS key for all users.

### 6. Images

```bash
./build.sh                                # every variant in variants/
./build.sh <variant>                      # one; reuses images built from the same SHAs
./bench.sh list                           # variants with build time and SHAs
```

First t-route compile ~5–10 min. Rerun after a variant's refs change. Images are shared through
Docker; users don't rebuild.

### 7. Run

```bash
tmux new -s bench
cd /data/troute_bench
TB_INHIBITED=none [VAR=value ...] ./bench.sh run <case> <variant> [<variant> ...]
```

- Run batches inside `tmux`; they survive a dropped SSH connection only there.
- `TB_INHIBITED=none`: skips `systemd-inhibit`, which asks polkit for a password outside a login
  session.
- Check `docker ps` before a batch: a killed `bench.sh` can leave a container running.
- Stopping: Ctrl-C, then `docker stop` any leftover container.

Options, cases and output: [`../README.md`](../README.md).

## USGS

1000 requests/h per key, shared by all users. `.env` defaults `USGS_CHUNK` to 40; long-run cases
set 5.

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

## Verify

```bash
id                                                     # groups include docker, bench
stat -c '%A %U:%G' /data                               # drwxrwsr-x root:bench
touch /data/t && stat -c '%G' /data/t && rm /data/t    # bench
docker info -f '{{.DockerRootDir}}'                    # /data/docker
grep -o avx2 /proc/cpuinfo | head -1                   # avx2
ssh -o PubkeyAuthentication=no benchvm true            # after disabling: Permission denied (publickey)
```
