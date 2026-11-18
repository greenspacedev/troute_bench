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

```bash
cd /data
git clone <repo-url> troute_bench && cd troute_bench
cp .env.example .env    # add HYDROTOOLS_USGS_API_KEY
./build.sh
./bench.sh run 03w_short baseline
```

- No cpufreq or hwmon in the guest: clock and temperature columns are empty.
- `hostinfo.py --check` passes (no battery or power profile to check). `powerprofilesctl` is not needed.

## Verify

```bash
id                                   # groups include docker, bench
stat -c '%A %U:%G' /data             # drwxrwsr-x root:bench
touch /data/t && stat -c '%G' /data/t && rm /data/t    # bench
ssh -o PubkeyAuthentication=no benchvm true            # after disabling: Permission denied (publickey)
```
