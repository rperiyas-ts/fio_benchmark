"""
XFS Filesystem Management
=========================
Functions for creating, mounting, and cleaning up XFS filesystems.
"""

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("fio_bench")

# Default XFS mount base path
XFS_MOUNT_BASE = "/mnt/xfs"


def _setup_xfs_single(device: str, mount_base: str = XFS_MOUNT_BASE) -> str:
    """
    Create XFS filesystem on a device and mount it.
    
    If XFS already exists on the device, skips mkfs and mounts directly.
    
    Args:
        device: Block device path (e.g., /dev/nvme1n1)
        mount_base: Base path for mount points (default: /mnt/xfs)
    
    Returns:
        Mount point path if successful
    
    Raises:
        RuntimeError: If mkfs or mount fails
    """
    dev_name = Path(device).name
    mount_point = f"{mount_base}_{dev_name}"
    
    # Check if already mounted
    if Path(mount_point).is_mount():
        log.info("XFS already mounted: %s → %s", device, mount_point)
        return mount_point
    
    # Check if XFS filesystem already exists on the device
    result = subprocess.run(
        ["blkid", "-o", "value", "-s", "TYPE", device],
        capture_output=True, text=True
    )
    existing_fs = result.stdout.strip()
    
    if existing_fs == "xfs":
        log.info("Existing XFS filesystem found on %s — skipping mkfs", device)
    else:
        if existing_fs:
            log.warning("Found %s filesystem on %s — will overwrite with XFS", 
                       existing_fs, device)
        log.info("Creating XFS filesystem on %s...", device)
        
        # Create XFS filesystem (force to overwrite any existing filesystem)
        result = subprocess.run(
            ["mkfs.xfs", "-f", device],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to create XFS on {device}: {result.stderr}"
            )
        log.info("XFS created on %s", device)
    
    # Create mount point directory
    Path(mount_point).mkdir(parents=True, exist_ok=True)
    
    # Mount the filesystem
    result = subprocess.run(
        ["mount", device, mount_point],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to mount {device} at {mount_point}: {result.stderr}"
        )
    log.info("Mounted %s → %s", device, mount_point)
    
    return mount_point


def setup_xfs(
    drives: list[str], 
    mount_base: str = XFS_MOUNT_BASE
) -> list[tuple[str, str]]:
    """
    Setup XFS filesystems on all provided drives.
    
    Args:
        drives: List of block device paths
        mount_base: Base path for mount points
    
    Returns:
        List of (dev_name, mount_point) tuples for successfully setup drives
    """
    valid_drives = []
    
    for device in drives:
        dev_name = Path(device).name
        try:
            mount_point = _setup_xfs_single(device, mount_base)
            valid_drives.append((dev_name, mount_point))
        except RuntimeError as e:
            log.error("Failed to setup XFS on %s: %s", device, e)
            continue
    
    return valid_drives


def _cleanup_xfs_single(device: str, mount_base: str = XFS_MOUNT_BASE) -> bool:
    """
    Unmount XFS filesystem from a device.
    
    Args:
        device: Block device path (e.g., /dev/nvme1n1)
        mount_base: Base path for mount points
    
    Returns:
        True if unmounted successfully or was not mounted, False on error
    """
    dev_name = Path(device).name
    mount_point = f"{mount_base}_{dev_name}"
    
    # Check if mounted
    if not Path(mount_point).is_mount():
        log.debug("XFS not mounted at %s — nothing to unmount", mount_point)
        return True
    
    log.info("Unmounting XFS: %s", mount_point)
    
    result = subprocess.run(
        ["umount", mount_point],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        log.error("Failed to unmount %s: %s", mount_point, result.stderr)
        return False
    
    log.info("Unmounted %s — drive %s ready for raw I/O", mount_point, device)
    return True


def cleanup_xfs(
    drives: list[str], 
    mount_base: str = XFS_MOUNT_BASE
) -> None:
    """
    Unmount XFS filesystems from all provided drives.
    
    Args:
        drives: List of block device paths
        mount_base: Base path for mount points
    """
    log.info("Cleaning up XFS mounts...")
    
    for device in drives:
        _cleanup_xfs_single(device, mount_base)
    
    log.info("XFS cleanup complete — drives available for raw I/O")
