from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def launch_native_preview(path: Path) -> str:
    """Open an image in the OS native full-resolution viewer without touching terminal stdio."""
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    spawn_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
    }

    if sys.platform == "darwin":
        if shutil.which("qlmanage"):
            subprocess.Popen(["qlmanage", "-p", str(path)], **spawn_kwargs)
            return "QuickLook"
        if shutil.which("open"):
            subprocess.Popen(["open", str(path)], **spawn_kwargs)
            return "Preview"
        raise RuntimeError("Neither qlmanage nor open is available on macOS")

    if os.name == "nt":
        startfile = getattr(os, "startfile", None)
        if startfile is not None:
            startfile(str(path))
            return "system viewer"
        raise RuntimeError("System file opener unavailable on Windows")

    if shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", str(path)], **spawn_kwargs)
        return "xdg-open"

    raise RuntimeError("No native image viewer found (xdg-open unavailable)")
