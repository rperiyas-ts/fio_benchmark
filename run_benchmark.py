#!/usr/bin/env python3
"""
FIO Storage Performance Benchmark Suite
========================================
Runs sequential and random I/O benchmarks against:
  - Raw block devices        : one fio job per drive, all 8 run in parallel
  - XFS filesystem           : one fio job per drive, all 8 run in parallel
                               (each drive has its own XFS)
  - ZFS pool                 : single fio job against the combined pool mountpoint

Profiles (iodepth=1, size=4TB each; runtime controlled centrally below):
  Seq Write  1MB | Seq Read  1MB | Seq Mixed 70R/30W 1MB
  Rand Write 4KB | Rand Read 4KB | Rand Mixed 70R/30W 4KB

Output: IOPs, avg clat, bandwidth — per-drive + aggregated totals
"""

import argparse
import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Import library functions
from lib import (
    get_os_drive,
    detect_data_nvme_drives,
    setup_xfs,
    cleanup_xfs,
    run_fio,
    build_job_file,
    extract_metrics,
    aggregate_metrics,
    format_table,
)

# ===========================================================================
# Configuration
# ===========================================================================

RUNTIME_SECONDS = 900           # 900 s = 15 minutes
XFS_MOUNT_BASE  = "/mnt/xfs"    # Mount layout: /mnt/xfs_nvme1n1, /mnt/xfs_nvme2n1, etc.
ZFS_MOUNT_POINT = "/bryck"

SCRIPT_DIR   = Path(__file__).parent.resolve()
PROFILES_DIR = SCRIPT_DIR / "profiles"
RESULTS_DIR  = SCRIPT_DIR / "results"
LOGS_DIR     = RESULTS_DIR

PROFILES = [
    "seq_write_1mb.fio",
    "seq_read_1mb.fio"
]
# Full profile list (uncomment to run all):
# PROFILES = [
#     "seq_write_1mb.fio",
#     "seq_read_1mb.fio",
#     "seq_mixed_1mb.fio",
#     "rand_write_4kb.fio",
#     "rand_read_4kb.fio",
#     "rand_mixed_4kb.fio",
# ]

PROFILE_LABELS = {
    "seq_write_1mb.fio":  "Seq Write  1MB",
    "seq_read_1mb.fio":   "Seq Read   1MB",
    "seq_mixed_1mb.fio":  "Seq Mixed  1MB (70R/30W)",
    "rand_write_4kb.fio": "Rand Write 4KB",
    "rand_read_4kb.fio":  "Rand Read  4KB",
    "rand_mixed_4kb.fio": "Rand Mixed 4KB (70R/30W)",
}

# ===========================================================================
# Logging
# ===========================================================================

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fio_bench")


# ---------------------------------------------------------------------------
# Benchmark runners
# ---------------------------------------------------------------------------

def run_raw(drives: list[str], runtime: int, results: dict, log_dir: Path) -> None:
    """Raw block: all 8 drives run in a single parallel fio invocation."""
    log.info("=" * 60)
    log.info("TIER: RAW BLOCK  (parallel, all drives)")
    log.info("=" * 60)
    results["raw"] = {}

    # Ensure no XFS filesystems are mounted on target drives
    log.info("Checking for mounted filesystems on target drives...")
    cleanup_xfs(drives, XFS_MOUNT_BASE)

    for profile_file in PROFILES:
        base  = PROFILES_DIR / "raw" / profile_file
        label = PROFILE_LABELS[profile_file]
        log.info("Profile: %s", label)

        targets  = {f"raw_{Path(dev).name}": dev for dev in drives}
        profile_name = profile_file.replace(".fio", "")
        job_file = build_job_file(base, targets, runtime, log_dir, "raw", profile_name)
        fio_out = run_fio(job_file, log_dir)

        per_drive = {}
        for job in fio_out.get("jobs", []):
            drv_name = job["jobname"].replace("raw_", "")
            per_drive[drv_name] = extract_metrics(job)

        results["raw"][profile_file] = {
            "per_drive":  per_drive,
            "aggregated": aggregate_metrics(list(per_drive.values())),
        }
        log.info("  Done. Aggregated IOPs: %.1f",
                 results["raw"][profile_file]["aggregated"].get("iops_total", 0))


def run_xfs(drives: list[str], runtime: int, results: dict, log_dir: Path) -> None:
    """XFS: all drives run in a single parallel fio invocation (like raw).
    
    Auto-creates XFS filesystems and mounts on drives that don't have them.
    Uses the same drive detection as raw tier (excludes OS boot drive).
    """
    log.info("=" * 60)
    log.info("TIER: XFS FILESYSTEM  (parallel, all drives)")
    log.info("=" * 60)
    results["xfs"] = {}

    # Auto-create XFS filesystems on drives that don't have mounts
    log.info("Setting up XFS filesystems on data drives...")
    valid_drives = setup_xfs(drives, XFS_MOUNT_BASE)

    if not valid_drives:
        log.error("No XFS filesystems could be created — skipping XFS tier entirely.")
        for p in PROFILES:
            results["xfs"][p] = {"per_drive": {}, "aggregated": {}}
        return
    
    log.info("XFS ready on %d drive(s): %s", 
             len(valid_drives), [d[0] for d in valid_drives])

    for profile_file in PROFILES:
        base  = PROFILES_DIR / "xfs" / profile_file
        label = PROFILE_LABELS[profile_file]
        log.info("Profile: %s", label)

        # Build targets for all valid drives (parallel execution)
        targets = {f"xfs_{dev_name}": f"{mount}/fio_test"
                   for dev_name, mount in valid_drives}
        profile_name = profile_file.replace(".fio", "")
        job_file = build_job_file(base, targets, runtime, log_dir, "xfs", profile_name)
        fio_out = run_fio(job_file, log_dir)

        per_drive = {}
        for job in fio_out.get("jobs", []):
            drv_name = job["jobname"].replace("xfs_", "")
            per_drive[drv_name] = extract_metrics(job)

        results["xfs"][profile_file] = {
            "per_drive":  per_drive,
            "aggregated": aggregate_metrics(list(per_drive.values())),
        }
        log.info("  Done. Aggregated IOPs: %.1f",
                 results["xfs"][profile_file]["aggregated"].get("iops_total", 0))
    
    # Cleanup: unmount XFS filesystems so drives can be used for raw I/O
    cleanup_xfs(drives, XFS_MOUNT_BASE)


def run_zfs(runtime: int, results: dict, log_dir: Path, zfs_mount: str) -> None:
    """ZFS: single fio job against the pool mountpoint."""
    log.info("=" * 60)
    log.info("TIER: ZFS POOL (mount: %s)", zfs_mount)
    log.info("=" * 60)
    results["zfs"] = {}

    if not Path(zfs_mount).is_mount():
        log.error("ZFS mount %s not found — skipping ZFS tier.", zfs_mount)
        for p in PROFILES:
            results["zfs"][p] = {"per_drive": {}, "aggregated": {}}
        return

    for profile_file in PROFILES:
        base  = PROFILES_DIR / "zfs" / profile_file
        label = PROFILE_LABELS[profile_file]
        log.info("Profile: %s", label)

        targets  = {"zfspool": f"{zfs_mount}/fio_test"}
        profile_name = profile_file.replace(".fio", "")
        job_file = build_job_file(base, targets, runtime, log_dir, "zfs", profile_name)
        fio_out = run_fio(job_file, log_dir)

        per_drive = {}
        for job in fio_out.get("jobs", []):
            per_drive["zfspool"] = extract_metrics(job)

        results["zfs"][profile_file] = {
            "per_drive":  per_drive,
            "aggregated": per_drive.get("zfspool", {}),
        }
        log.info("  Done. IOPs: %.1f",
                 results["zfs"][profile_file]["aggregated"].get("iops_total", 0))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="FIO Storage Benchmark Suite",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--tiers", nargs="+", choices=["raw", "xfs", "zfs"],
        default=["raw", "xfs", "zfs"],
        help="Tiers to benchmark",
    )
    p.add_argument(
        "--drives", nargs="+", default=None,
        help="Emergency override: manually specify drives (auto-detect is default)",
    )
    p.add_argument(
        "--runtime", type=int, default=RUNTIME_SECONDS,
        help="Runtime per profile in seconds (overrides RUNTIME_SECONDS)",
    )
    p.add_argument(
        "--zfs-mount", type=str, default=ZFS_MOUNT_POINT,
        help="ZFS pool mount point path",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print a sample generated job file and exit without running fio",
    )
    return p.parse_args()


def main():
    args    = parse_args()
    runtime = args.runtime

    if shutil.which("fio") is None:
        log.error("fio not found. Install: apt install fio  |  dnf install fio")
        sys.exit(1)

    # Drive selection: auto-detect by default, CLI override as emergency fallback
    if args.drives:
        # Emergency manual override — still guard against OS drive
        os_drive = f"/dev/{get_os_drive()}"
        if os_drive in args.drives:
            log.error("ABORT: OS drive %s is in --drives list!", os_drive)
            sys.exit(1)
        drives = args.drives
        log.info("Drive source         : CLI override (manual)")
    else:
        drives = detect_data_nvme_drives()
        log.info("Drive source         : auto-detected NVMe data drives")

    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Create timestamped logs directory for fio files and results
    log_dir = LOGS_DIR / f"run_{ts}"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    results = {}

    log.info("Drives selected      : %s", drives)
    log.info("Tiers                : %s", args.tiers)
    log.info("Runtime              : %d s (%d min)", runtime, runtime // 60)
    log.info("Profile dir          : %s", PROFILES_DIR)
    log.info("Logs dir             : %s", log_dir)

    # -- Dry-run: show the generated job file for the first raw profile -----
    if args.dry_run:
        log.info("DRY RUN — generated job file for raw/%s:", PROFILES[0])
        sample   = PROFILES_DIR / "raw" / PROFILES[0]
        targets  = {f"raw_{Path(d).name}": d for d in drives}
        job_file = build_job_file(sample, targets, runtime)
        with open(job_file) as f:
            print(f.read())
        os.unlink(job_file)
        return

    if "raw" in args.tiers:
        run_raw(drives, runtime, results, log_dir)

    if "xfs" in args.tiers:
        run_xfs(drives, runtime, results, log_dir)

    if "zfs" in args.tiers:
        run_zfs(runtime, results, log_dir, args.zfs_mount)

    # -- Persist results to logs directory ----------------------------------
    json_path = log_dir / f"fio_results_{ts}.json"
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2)
    log.info("JSON results  → %s", json_path)

    table    = format_table(results, runtime, PROFILE_LABELS)
    txt_path = log_dir / f"fio_results_{ts}.txt"
    with open(txt_path, "w") as f:
        f.write(table)
    log.info("Text report   → %s", txt_path)

    print(table)


if __name__ == "__main__":
    main()