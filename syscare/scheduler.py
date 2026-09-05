"""User-systemd schedule for safe, headless SysCare maintenance."""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from .util import run, which, xdg_config_home


UNIT = "syscare-maintenance.service"
TIMER = "syscare-maintenance.timer"
CALENDARS = {
    "daily": "daily",
    "weekly": "Sun *-*-* 10:00:00",
    "monthly": "*-*-01 10:00:00",
}


@dataclass(frozen=True)
class ScheduleStatus:
    enabled: bool
    frequency: str
    next_run: str


def unit_dir() -> Path:
    return xdg_config_home() / "systemd" / "user"


def set_schedule(frequency: str) -> tuple[bool, str]:
    if frequency not in CALENDARS:
        return False, f"Unsupported schedule: {frequency}"
    systemctl = which("systemctl")
    if not systemctl:
        return False, "systemctl not found"
    target = unit_dir()
    target.mkdir(parents=True, exist_ok=True)
    command = _syscare_command()
    service = (
        "[Unit]\nDescription=SysCare safe scheduled maintenance\n\n"
        "[Service]\nType=oneshot\n"
        f"ExecStart={command} --clean-safe --scheduled\n"
    )
    timer = (
        "[Unit]\nDescription=Run SysCare safe maintenance\n\n"
        "[Timer]\n"
        f"OnCalendar={CALENDARS[frequency]}\n"
        "Persistent=true\nRandomizedDelaySec=15m\nUnit=syscare-maintenance.service\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    (target / UNIT).write_text(service, encoding="utf-8")
    (target / TIMER).write_text(timer, encoding="utf-8")
    result = run([systemctl, "--user", "daemon-reload"], timeout=30)
    if result.returncode == 0:
        result = run([systemctl, "--user", "enable", "--now", TIMER], timeout=30)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "Could not enable timer").strip()
    return True, f"{frequency.capitalize()} maintenance enabled"


def disable_schedule() -> tuple[bool, str]:
    systemctl = which("systemctl")
    if not systemctl:
        return False, "systemctl not found"
    result = run([systemctl, "--user", "disable", "--now", TIMER], timeout=30)
    if result.returncode not in (0, 1):
        return False, (result.stderr or result.stdout or "Could not disable timer").strip()
    return True, "Scheduled maintenance disabled"


def get_status() -> ScheduleStatus:
    systemctl = which("systemctl")
    if not systemctl:
        return ScheduleStatus(False, "off", "")
    enabled_result = run([systemctl, "--user", "is-enabled", TIMER], timeout=15)
    enabled = enabled_result.returncode == 0 and (enabled_result.stdout or "").strip() == "enabled"
    timer_text = ""
    try:
        timer_text = (unit_dir() / TIMER).read_text(encoding="utf-8")
    except OSError:
        pass
    frequency = "off"
    for name, calendar in CALENDARS.items():
        if f"OnCalendar={calendar}" in timer_text:
            frequency = name
            break
    next_run = ""
    if enabled:
        result = run(
            [systemctl, "--user", "show", TIMER, "-p", "NextElapseUSecRealtime", "--value"],
            timeout=15,
        )
        next_run = (result.stdout or "").strip()
    return ScheduleStatus(enabled, frequency, next_run)


def _syscare_command() -> str:
    local = Path.home() / ".local/bin/syscare"
    if local.is_file() and os.access(local, os.X_OK):
        return shlex.quote(str(local))
    system = Path("/usr/bin/syscare")
    if system.is_file() and os.access(system, os.X_OK):
        return str(system)
    launcher = Path(sys.argv[0]).expanduser()
    if launcher.is_file() and os.access(launcher, os.X_OK):
        return shlex.quote(str(launcher.resolve()))
    return f"{shlex.quote(sys.executable)} -m syscare"

