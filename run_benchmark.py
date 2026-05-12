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
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# ===========================================================================
# !! EDIT THIS SECTION TO MATCH YOUR ENVIRONMENT !!
# ===========================================================================

# --- Runtime (seconds) applied to every profile across all tiers ----------
RUNTIME_SECONDS = 10          # 900 s = 15 minutes

# --- Block devices ---------------------------------------------------------
DRIVES = [
    "/dev/nvme1n1", "/dev/nvme2n1", "/dev/nvme3n1", "/dev/nvme4n1"
]
# DRIVES = [
#     "/dev/nvme1n1", "/dev/nvme2n1", "/dev/nvme3n1", "/dev/nvme4n1",
#     "/dev/nvme6n1", "/dev/nvme7n1", "/dev/nvme8n1", "/dev/nvme9n1",
# ]


# --- XFS: each drive is independently formatted and mounted ----------------
#   Mount layout expected: /mnt/xfs_nvme1n1, /mnt/xfs_nvme2n1, etc.
XFS_MOUNT_BASE = "/mnt/xfs"

# --- ZFS: all 8 drives combined into a single pool -------------------------
ZFS_POOL_NAME   = "benchpool"
ZFS_MOUNT_POINT = "/mnt/zfspool"

# ===========================================================================
# (no edits needed below this line for standard usage)
# ===========================================================================

SCRIPT_DIR   = Path(__file__).parent.resolve()
PROFILES_DIR = SCRIPT_DIR / "profiles"
RESULTS_DIR  = SCRIPT_DIR / "results"
LOGS_DIR     = RESULTS_DIR

PROFILES = [
    "seq_write_1mb.fio",
    "seq_read_1mb.fio"
]
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fio_bench")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_fio(job_file: str, log_dir: Path = None) -> dict:
    """Execute fio and return parsed JSON output.
    
    If log_dir is provided, saves the complete fio output to a file
    named after the job file (e.g., raw_seq_write_1mb_output.json).
    """
    cmd = ["fio", "--output-format=json", job_file]
    log.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("fio failed:\n%s", result.stderr)
        raise RuntimeError(f"fio exited {result.returncode}")
    
    # Save complete fio output if log_dir provided
    if log_dir:
        job_name = Path(job_file).stem  # e.g., raw_seq_write_1mb
        output_file = log_dir / f"{job_name}_output.json"
        with open(output_file, "w") as f:
            f.write(result.stdout)
        log.debug("Complete fio output → %s", output_file)
    
    return json.loads(result.stdout)


def build_job_file(
    base_profile: Path,
    targets: dict[str, str],
    runtime: int,
    log_dir: Path = None,
    tier: str = "",
    profile_name: str = "",
) -> str:
    """
    Merge a base profile with per-target [job] sections.
    The runtime line in the profile is replaced with the central RUNTIME_SECONDS
    value so profiles themselves never need editing.

    targets      : {job_name: device_or_filepath}
    log_dir      : directory to save the generated .fio file (if None, uses temp)
    tier         : tier name for the filename (e.g., 'raw', 'xfs', 'zfs')
    profile_name : profile name for the filename (e.g., 'seq_write_1mb')
    Returns      : path to the generated .fio file.
    """
    with open(base_profile) as f:
        lines = f.readlines()

    cleaned = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):          # drop comment lines
            continue
        if stripped.startswith("runtime="):  # override with central value
            cleaned.append(f"runtime={runtime}\n")
        else:
            cleaned.append(line)

    base_content = "".join(cleaned)

    job_sections = "\n".join(
        f"\n[{name}]\nfilename={target}" for name, target in targets.items()
    )

    content = base_content + "\n" + job_sections + "\n"

    if log_dir:
        # Save to logs directory with meaningful name
        filename = f"{tier}_{profile_name}.fio" if tier and profile_name else "bench.fio"
        job_path = log_dir / filename
        with open(job_path, "w") as f:
            f.write(content)
        return str(job_path)
    else:
        # Fallback to temp file (for dry-run or legacy usage)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".fio", delete=False, prefix="bench_"
        )
        tmp.write(content)
        tmp.flush()
        tmp.close()
        return tmp.name


def extract_metrics(job: dict) -> dict:
    """Return IOPs, avg completion latency (µs), and bandwidth (MiB/s)."""
    read  = job.get("read",  {})
    write = job.get("write", {})

    def safe(d, *keys):
        v = d
        for k in keys:
            v = v.get(k, {}) if isinstance(v, dict) else {}
        return v if not isinstance(v, dict) else 0

    iops_r = safe(read,  "iops")
    iops_w = safe(write, "iops")
    bw_r   = safe(read,  "bw")          # KiB/s
    bw_w   = safe(write, "bw")
    clat_r = safe(read,  "clat_ns", "mean")
    clat_w = safe(write, "clat_ns", "mean")

    total_iops = (iops_r or 0) + (iops_w or 0)
    total_bw   = (bw_r   or 0) + (bw_w   or 0)

    if total_iops > 0:
        avg_clat_ns = (
            (clat_r or 0) * (iops_r or 0) +
            (clat_w or 0) * (iops_w or 0)
        ) / total_iops
    else:
        avg_clat_ns = 0

    return {
        "iops_read":    round(iops_r or 0, 1),
        "iops_write":   round(iops_w or 0, 1),
        "iops_total":   round(total_iops,  1),
        "bw_read_mbs":  round((bw_r or 0) / 1024, 2),
        "bw_write_mbs": round((bw_w or 0) / 1024, 2),
        "bw_total_mbs": round(total_bw     / 1024, 2),
        "avg_clat_us":  round(avg_clat_ns  / 1000, 2),
    }


def aggregate_metrics(metrics_list: list[dict]) -> dict:
    """Sum IOPs and BW across drives; mean clat."""
    if not metrics_list:
        return {}
    keys_sum = ["iops_read", "iops_write", "iops_total",
                 "bw_read_mbs", "bw_write_mbs", "bw_total_mbs"]
    agg = {k: round(sum(m[k] for m in metrics_list), 2) for k in keys_sum}
    agg["avg_clat_us"] = round(
        sum(m["avg_clat_us"] for m in metrics_list) / len(metrics_list), 2
    )
    return agg


def format_table(results: dict, runtime: int) -> str:
    """Render benchmark results as a human-readable fixed-width table."""
    col_w = 12
    sep   = "-" * 116

    header = (
        f"{'Drive/Target':<24} {'IOPs R':>{col_w}} {'IOPs W':>{col_w}} "
        f"{'IOPs Tot':>{col_w}} {'BW R MB/s':>{col_w}} {'BW W MB/s':>{col_w}} "
        f"{'BW Tot MB/s':>{col_w}} {'Avg Clat µs':>{col_w}}"
    )

    def row(label, m):
        return (
            f"{label:<24} {m['iops_read']:>{col_w}.1f} {m['iops_write']:>{col_w}.1f} "
            f"{m['iops_total']:>{col_w}.1f} {m['bw_read_mbs']:>{col_w}.2f} "
            f"{m['bw_write_mbs']:>{col_w}.2f} {m['bw_total_mbs']:>{col_w}.2f} "
            f"{m['avg_clat_us']:>{col_w}.2f}"
        )

    lines = [
        "",
        "=" * 116,
        f"  FIO BENCHMARK RESULTS   runtime={runtime}s  iodepth=1  size=4T",
        "=" * 116,
        sep, header, sep,
    ]

    for tier, profiles in results.items():
        lines += ["", f"{'':=<116}", f"  TIER: {tier.upper()}", f"{'':=<116}"]
        for profile_name, drive_data in profiles.items():
            lines += [f"\n  {PROFILE_LABELS.get(profile_name, profile_name)}", sep]
            per_drive = drive_data.get("per_drive", {})
            for drv, m in per_drive.items():
                lines.append(row(f"    {drv}", m))
            if "aggregated" in drive_data and drive_data["aggregated"]:
                lines.append(sep)
                lines.append(row("  *** AGGREGATED ***", drive_data["aggregated"]))
            lines.append(sep)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Benchmark runners
# ---------------------------------------------------------------------------

def run_raw(drives: list[str], runtime: int, results: dict, log_dir: Path) -> None:
    """Raw block: all 8 drives run in a single parallel fio invocation."""
    log.info("=" * 60)
    log.info("TIER: RAW BLOCK  (parallel, all drives)")
    log.info("=" * 60)
    results["raw"] = {}

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
    """XFS: all drives run in a single parallel fio invocation (like raw)."""
    log.info("=" * 60)
    log.info("TIER: XFS FILESYSTEM  (parallel, all drives)")
    log.info("=" * 60)
    results["xfs"] = {}

    # Validate mounts up-front
    valid_drives = []
    for dev in drives:
        dev_name = Path(dev).name
        mount    = f"{XFS_MOUNT_BASE}_{dev_name}"
        if Path(mount).is_mount():
            valid_drives.append((dev_name, mount))
        else:
            log.warning("XFS mount %s not found — skipping %s", mount, dev)

    if not valid_drives:
        log.error("No XFS mounts available — skipping XFS tier entirely.")
        for p in PROFILES:
            results["xfs"][p] = {"per_drive": {}, "aggregated": {}}
        return

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


def run_zfs(runtime: int, results: dict, log_dir: Path) -> None:
    """ZFS: single fio job against the pool mountpoint."""
    log.info("=" * 60)
    log.info("TIER: ZFS POOL (%s)", ZFS_POOL_NAME)
    log.info("=" * 60)
    results["zfs"] = {}

    if not Path(ZFS_MOUNT_POINT).is_mount():
        log.error("ZFS mount %s not found — skipping ZFS tier.", ZFS_MOUNT_POINT)
        for p in PROFILES:
            results["zfs"][p] = {"per_drive": {}, "aggregated": {}}
        return

    for profile_file in PROFILES:
        base  = PROFILES_DIR / "zfs" / profile_file
        label = PROFILE_LABELS[profile_file]
        log.info("Profile: %s", label)

        targets  = {"zfspool": f"{ZFS_MOUNT_POINT}/fio_test"}
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
        "--drives", nargs="+", default=DRIVES,
        help="Block devices to benchmark",
    )
    p.add_argument(
        "--runtime", type=int, default=RUNTIME_SECONDS,
        help="Runtime per profile in seconds (overrides RUNTIME_SECONDS)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print a sample generated job file and exit without running fio",
    )
    return p.parse_args()


def main():
    args    = parse_args()
    drives  = args.drives
    runtime = args.runtime

    if shutil.which("fio") is None:
        log.error("fio not found. Install: apt install fio  |  dnf install fio")
        sys.exit(1)

    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Create timestamped logs directory for fio files and results
    log_dir = LOGS_DIR / f"run_{ts}"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    results = {}

    log.info("Drives        : %s", drives)
    log.info("Tiers         : %s", args.tiers)
    log.info("Runtime       : %d s (%d min)", runtime, runtime // 60)
    log.info("Profile dir   : %s", PROFILES_DIR)
    log.info("Logs dir      : %s", log_dir)

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
        run_zfs(runtime, results, log_dir)

    # -- Persist results to logs directory ----------------------------------
    json_path = log_dir / f"fio_results_{ts}.json"
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2)
    log.info("JSON results  → %s", json_path)

    table    = format_table(results, runtime)
    txt_path = log_dir / f"fio_results_{ts}.txt"
    with open(txt_path, "w") as f:
        f.write(table)
    log.info("Text report   → %s", txt_path)

    print(table)


if __name__ == "__main__":
    main()