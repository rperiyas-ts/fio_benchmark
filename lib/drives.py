"""
Drive Detection Utilities
=========================
Functions for detecting NVMe drives and identifying the OS boot drive.
"""

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("fio_bench")


def get_os_drive() -> str:
    """Return base device name of the OS drive (e.g. 'nvme0n1')."""
    root_src = subprocess.run(
        ["findmnt", "-n", "-o", "SOURCE", "/"],
        capture_output=True, text=True, check=True
    ).stdout.strip()
    # root_src could be a partition (/dev/nvme0n1p2) or LVM — walk up to disk
    base = subprocess.run(
        ["lsblk", "-no", "pkname", root_src],
        capture_output=True, text=True, check=True
    ).stdout.strip()
    return base  # e.g. 'nvme0n1'


def detect_data_nvme_drives() -> list[str]:
    """
    Auto-select all NVMe block disks that are NOT the OS/boot drive.
    No manual input needed — purely driven by what lsblk and findmnt report.
    """
    os_drive = get_os_drive()
    log.info("OS drive detected (excluded) : /dev/%s", os_drive)

    # All NVMe block disks on the system
    result = subprocess.run(
        ["lsblk", "-dno", "NAME,TYPE"],
        capture_output=True, text=True, check=True
    )

    all_nvme, data_drives = [], []

    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "disk" and parts[0].startswith("nvme"):
            dev = f"/dev/{parts[0]}"
            all_nvme.append(dev)
            if parts[0] != os_drive:
                data_drives.append(dev)

    log.info("All NVMe drives found        : %s", all_nvme)
    log.info("Data drives selected         : %s", data_drives)

    if not data_drives:
        raise RuntimeError(
            f"No data NVMe drives found after excluding OS drive "
            f"(/dev/{os_drive}). All NVMe: {all_nvme}"
        )

    # Final safety check — hard abort if OS drive somehow slipped through
    for dev in data_drives:
        if Path(dev).name == os_drive:
            raise RuntimeError(
                f"SAFETY ABORT: OS drive /dev/{os_drive} was about to be "
                f"included in benchmark targets. Aborting."
            )

    return data_drives
