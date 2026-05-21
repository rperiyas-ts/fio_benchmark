"""
FIO Execution and Metrics
=========================
Functions for running FIO benchmarks and processing results.
"""

import json
import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("fio_bench")


def run_fio(job_file: str, log_dir: Path = None) -> dict:
    """Execute fio and return parsed JSON output.
    
    If log_dir is provided, saves the complete fio output to a file
    named after the job file (e.g., raw_seq_write_1mb_output.json).
    
    Args:
        job_file: Path to the FIO job file
        log_dir: Optional directory to save raw FIO output
    
    Returns:
        Parsed JSON output from FIO
    
    Raises:
        RuntimeError: If FIO execution fails
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
    
    The runtime line in the profile is replaced with the provided runtime
    value so profiles themselves never need editing.

    Args:
        base_profile: Path to the base .fio profile
        targets: {job_name: device_or_filepath}
        runtime: Runtime in seconds for each profile
        log_dir: Directory to save the generated .fio file (if None, uses temp)
        tier: Tier name for the filename (e.g., 'raw', 'xfs', 'zfs')
        profile_name: Profile name for the filename (e.g., 'seq_write_1mb')
    
    Returns:
        Path to the generated .fio file.
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
    """
    Extract performance metrics from a FIO job result.
    
    Args:
        job: Single job result from FIO JSON output
    
    Returns:
        Dictionary with IOPs, avg completion latency (µs), and bandwidth (MiB/s)
    """
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
    """
    Aggregate metrics across multiple drives.
    
    Args:
        metrics_list: List of metric dictionaries from extract_metrics()
    
    Returns:
        Aggregated metrics (sum IOPs and BW, mean clat)
    """
    if not metrics_list:
        return {}
    keys_sum = ["iops_read", "iops_write", "iops_total",
                 "bw_read_mbs", "bw_write_mbs", "bw_total_mbs"]
    agg = {k: round(sum(m[k] for m in metrics_list), 2) for k in keys_sum}
    agg["avg_clat_us"] = round(
        sum(m["avg_clat_us"] for m in metrics_list) / len(metrics_list), 2
    )
    return agg


def format_table(results: dict, runtime: int, profile_labels: dict) -> str:
    """
    Render benchmark results as a human-readable fixed-width table.
    
    Args:
        results: Benchmark results dictionary
        runtime: Runtime used for benchmarks
        profile_labels: Mapping of profile filenames to display labels
    
    Returns:
        Formatted table string
    """
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
            lines += [f"\n  {profile_labels.get(profile_name, profile_name)}", sep]
            per_drive = drive_data.get("per_drive", {})
            for drv, m in per_drive.items():
                lines.append(row(f"    {drv}", m))
            if "aggregated" in drive_data and drive_data["aggregated"]:
                lines.append(sep)
                lines.append(row("  *** AGGREGATED ***", drive_data["aggregated"]))
            lines.append(sep)

    return "\n".join(lines)
