"""
FIO Benchmark Library
=====================
Utility modules for the FIO storage benchmark suite.
"""

from .drives import get_os_drive, detect_data_nvme_drives
from .xfs import (
    setup_xfs,
    cleanup_xfs,
)
from .fio import (
    run_fio,
    build_job_file,
    extract_metrics,
    aggregate_metrics,
    format_table,
)

__all__ = [
    # drives
    "get_os_drive",
    "detect_data_nvme_drives",
    # xfs
    "setup_xfs",
    "cleanup_xfs",
    # fio
    "run_fio",
    "build_job_file",
    "extract_metrics",
    "aggregate_metrics",
    "format_table",
]
