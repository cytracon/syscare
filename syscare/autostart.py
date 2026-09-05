"""Optional XDG autostart for SysCare tray on login."""

from __future__ import annotations

from pathlib import Path

from . import __app_id__, __app_name__
from .util import which, xdg_config_home

AUTOSTART_NAME = f"{__app_id__}-tray.desktop"


def autostart_path() -> Path:
    return xdg_config_home() / "autostart" / AUTOSTART_NAME


def is_enabled() -> bool:
    p = autostart_path()
    if not p.is_file():
        return False
    text = p.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if line.strip().lower() == "hidden=true":
            return False
        if line.strip().lower() == "x-gnome-autostart-enabled=false":
            return False
    return True


def _syscare_exec() -> str:
    # Prefer real binary on PATH
    w = which("syscare")
    if w:
        return w
    local = Path.home() / ".local/bin/syscare"
    if local.is_file():
        return str(local)
    return "/usr/bin/syscare"


def enable() -> tuple[bool, str]:
    """Install ~/.config/autostart entry: syscare --tray on login."""
    dest = autostart_path()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        exe = _syscare_exec()
        content = f"""[Desktop Entry]
Type=Application
Version=1.0
Name={__app_name__}
Comment=System optimizer tray (start hidden)
Exec={exe} --tray
Icon={__app_id__}
Terminal=false
Categories=System;Monitor;
X-GNOME-Autostart-enabled=true
Hidden=false
StartupNotify=false
"""
        dest.write_text(content, encoding="utf-8")
        return True, str(dest)
    except OSError as e:
        return False, str(e)


def disable() -> tuple[bool, str]:
    p = autostart_path()
    if not p.exists():
        return True, "already off"
    try:
        p.unlink()
        return True, f"removed {p}"
    except OSError as e:
        return False, str(e)


def status_text() -> str:
    if is_enabled():
        return f"on ({autostart_path()})"
    return "off"
