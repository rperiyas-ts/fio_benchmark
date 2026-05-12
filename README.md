# FIO Storage Benchmark Suite

Benchmarks raw block, XFS, and ZFS performance across 8 drives using 6 I/O profiles.

---

## Directory Layout

```
fio_benchmark/
├── run_benchmark.py          # Main orchestration script
├── profiles/
│   ├── raw/                  # Raw block device profiles
│   │   ├── seq_write_1mb.fio
│   │   ├── seq_read_1mb.fio
│   │   ├── seq_mixed_1mb.fio
│   │   ├── rand_write_4kb.fio
│   │   ├── rand_read_4kb.fio
│   │   └── rand_mixed_4kb.fio
│   ├── xfs/                  # XFS filesystem profiles (same 6)
│   └── zfs/                  # ZFS pool profiles (same 6)
└── results/                  # Timestamped run directories
    └── run_YYYYMMDD_HHMMSS/  # Each benchmark run
        ├── raw_*.fio         # Generated fio job files
        ├── xfs_*.fio
        ├── zfs_*.fio
        ├── fio_results_*.json
        └── fio_results_*.txt
```

---

## Prerequisites

```bash
# Debian/Ubuntu
apt install fio

# RHEL/Fedora
dnf install fio
```

---

## Setup Before Running

### 1. Edit drive list in `run_benchmark.py`
```python
DRIVES = [
    "/dev/sda", "/dev/sdb", "/dev/sdc", "/dev/sdd",
    "/dev/sde", "/dev/sdf", "/dev/sdg", "/dev/sdh",
]
```

### 2. Mount XFS filesystems
Create and mount one XFS per drive. Script expects `/mnt/xfs_<devname>`:

```bash
for dev in sda sdb sdc sdd sde sdf sdg sdh; do
    sudo mkfs.xfs -f /dev/$dev
    sudo mkdir -p /mnt/xfs_$dev
    sudo mount /dev/$dev /mnt/xfs_$dev
done
```

### 3. Create and mount ZFS pool
Script expects the pool mounted at `/mnt/zfspool`:
Do create the zfs mount using bryck UI, don't use below cmds.

```bash
# Stripe (best performance baseline)
zpool create benchpool /dev/sda /dev/sdb /dev/sdc /dev/sdd \
                       /dev/sde /dev/sdf /dev/sdg /dev/sdh

# Or mirror, raidz, raidz2 etc. depending on your use case
zfs set mountpoint=/mnt/zfspool benchpool
```

---

## Running

```bash
# All tiers (raw + xfs + zfs) — ~2.25 hours total
sudo python3 run_benchmark.py

# Specific tiers only
sudo python3 run_benchmark.py --tiers raw
sudo python3 run_benchmark.py --tiers xfs
sudo python3 run_benchmark.py --tiers zfs

# Override drive list on the CLI
sudo python3 run_benchmark.py --drives /dev/nvme0n1 /dev/nvme1n1

# Preview generated job file without running fio
sudo python3 run_benchmark.py --dry-run
```

---

## Profiles Summary

| Profile File       | I/O Pattern          | Block Size | Runtime |
|--------------------|----------------------|------------|---------|
| seq_write_1mb.fio  | Sequential Write     | 1 MB       | 15 min  |
| seq_read_1mb.fio   | Sequential Read      | 1 MB       | 15 min  |
| seq_mixed_1mb.fio  | Seq Mixed 70R/30W    | 1 MB       | 15 min  |
| rand_write_4kb.fio | Random Write         | 4 KB       | 15 min  |
| rand_read_4kb.fio  | Random Read          | 4 KB       | 15 min  |
| rand_mixed_4kb.fio | Rand Mixed 70R/30W   | 4 KB       | 15 min  |

All profiles share: `iodepth=1`, `size=4T`, `direct=1`, `ioengine=libaio`

---

## Output

Results are saved to timestamped subdirectories under `results/`:

```
results/
└── run_20250504_143000/
    ├── raw_seq_write_1mb.fio      # Generated fio job files
    ├── raw_seq_read_1mb.fio
    ├── raw_seq_mixed_1mb.fio
    ├── raw_rand_write_4kb.fio
    ├── raw_rand_read_4kb.fio
    ├── raw_rand_mixed_4kb.fio
    ├── xfs_seq_write_1mb.fio      # (if xfs tier selected)
    ├── ...
    ├── fio_results_20250504_143000.json
    └── fio_results_20250504_143000.txt
```

The generated `.fio` files show the exact job configuration used for each
benchmark run, useful for debugging or manual re-runs.

### Table columns

| Column       | Description                          |
|--------------|--------------------------------------|
| IOPs R       | Read IOPS                            |
| IOPs W       | Write IOPS                           |
| IOPs Tot     | Total IOPS (R + W)                   |
| BW R MB/s    | Read bandwidth in MiB/s              |
| BW W MB/s    | Write bandwidth in MiB/s             |
| BW Tot MB/s  | Total bandwidth                      |
| Avg Clat µs  | Average completion latency (µs)      |

Per-drive rows are shown first, followed by an **AGGREGATED** row summing
IOPs/BW and averaging clat across all drives in that tier/profile.

---

## Notes

- **RAW benchmark** writes directly to the block device — ensure no filesystem
  is mounted on those devices and data loss is acceptable.
- **ZFS** disables the ARC for file benchmarks with `direct=1`; this gives
  realistic disk-level numbers rather than cache hits.
- Run as root (`sudo`) since direct block I/O requires elevated privileges.
- Total runtime ≈ 6 profiles × 15 min × 3 tiers = **~4.5 hours**.
  Use `--tiers` to run subsets.
