"""USB stick detection and formula import."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from .config import UsbConfig
from .formulas import FormulaError, Formula

log = logging.getLogger(__name__)


def find_usb_mount(cfg: UsbConfig = UsbConfig(), mounts_file: str = "/proc/mounts") -> Optional[Path]:
    """Return the mountpoint of a mounted USB volume, if any."""
    try:
        lines = Path(mounts_file).read_text().splitlines()
    except OSError:
        return None
    roots = tuple(cfg.mount_roots)
    for line in lines:
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith(cfg.device_prefix):
            continue
        mountpoint = parts[1].replace("\\040", " ")
        if mountpoint.startswith(roots):
            return Path(mountpoint)
    return None


class UsbMonitor:
    """Polled; reports insertion/removal edges."""

    def __init__(self, finder=find_usb_mount):
        self._finder = finder
        self.mount: Optional[Path] = None

    def poll(self) -> tuple:
        """Returns ``(inserted, removed)`` mount paths (each may be None)."""
        current = self._finder()
        previous, self.mount = self.mount, current
        if current == previous:
            return None, None
        return current, previous


def import_formulas(usb: Path, dest: Path, folder: str = "formulas") -> int:
    """Copy valid ``<usb>/<folder>/*.txt`` into ``dest``. Returns number imported."""
    src = usb / folder
    if not src.is_dir():
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted(src.glob("*.txt")):
        if path.name.startswith("."):  # macOS "._foo.txt" resource forks
            continue
        try:
            Formula.parse(path.stem, path.read_text(encoding="utf-8"))
            shutil.copyfile(path, dest / path.name)
            count += 1
        except (FormulaError, OSError, UnicodeDecodeError) as exc:
            log.warning("not importing %s: %s", path, exc)
    return count
