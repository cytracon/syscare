"""XDG autostart applications."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .util import iter_autostart_dirs, parse_env_file_desktop, run, which, xdg_config_home


@dataclass
class StartupApp:
    name: str
    exec_cmd: str
    comment: str
    path: Path
    enabled: bool
    system: bool  # True if under /etc
    source: str = "xdg"  # xdg | systemd-user
    unit: str = ""


def list_startup_apps() -> list[StartupApp]:
    apps: list[StartupApp] = []
    seen_names: set[str] = set()
    for d in iter_autostart_dirs():
        if not d.is_dir():
            continue
        system = str(d).startswith("/etc")
        for f in sorted(d.glob("*.desktop")):
            data = parse_env_file_desktop(f)
            if not data:
                continue
            name = data.get("Name") or f.stem
            # user overrides hide system ones with same basename in UI later
            key = f.name
            if key in seen_names:
                continue
            seen_names.add(key)
            hidden = data.get("Hidden", "").lower() == "true"
            only_show = data.get("OnlyShowIn", "")
            not_show = data.get("NotShowIn", "")
            # treat X-GNOME-Autostart-enabled=false as disabled
            g_en = data.get("X-GNOME-Autostart-enabled", "true").lower()
            enabled = not hidden and g_en != "false"
            apps.append(
                StartupApp(
                    name=name,
                    exec_cmd=data.get("Exec", ""),
                    comment=data.get("Comment", ""),
                    path=f,
                    enabled=enabled,
                    system=system,
                )
            )
    systemctl = which("systemctl")
    if systemctl:
        try:
            result = run(
                [systemctl, "--user", "list-unit-files", "--type=service", "--no-pager", "--plain"],
                timeout=20,
            )
            for line in (result.stdout or "").splitlines():
                columns = line.split()
                if len(columns) < 2 or not columns[0].endswith(".service"):
                    continue
                unit, state = columns[:2]
                if state not in {"enabled", "enabled-runtime", "disabled"}:
                    continue
                apps.append(
                    StartupApp(
                        name=unit.removesuffix(".service"),
                        exec_cmd=unit,
                        comment="systemd user service",
                        path=Path(unit),
                        enabled=state.startswith("enabled"),
                        system=False,
                        source="systemd-user",
                        unit=unit,
                    )
                )
        except Exception:  # noqa: BLE001
            pass
    apps.sort(key=lambda a: (not a.enabled, a.name.lower()))
    return apps


def set_startup_enabled(app: StartupApp, enabled: bool) -> tuple[bool, str]:
    """Enable/disable. System entries are overridden via ~/.config/autostart copy."""
    if app.source == "systemd-user":
        systemctl = which("systemctl")
        if not systemctl or not app.unit.endswith(".service") or "/" in app.unit:
            return False, "Invalid or unavailable systemd user unit"
        action = "enable" if enabled else "disable"
        result = run([systemctl, "--user", action, app.unit], timeout=30)
        return result.returncode == 0, (result.stderr or result.stdout or f"{action}d").strip()

    path = app.path
    user_dir = xdg_config_home() / "autostart"
    user_dir.mkdir(parents=True, exist_ok=True)

    if app.system:
        # Write override in user autostart
        dest = user_dir / path.name
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return False, str(e)
        text = _set_desktop_bool(text, "Hidden", not enabled)
        text = _set_desktop_key(text, "X-GNOME-Autostart-enabled", "true" if enabled else "false")
        try:
            dest.write_text(text, encoding="utf-8")
            return True, f"Wrote override {dest}"
        except OSError as e:
            return False, str(e)

    # User-owned file
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        text = _set_desktop_bool(text, "Hidden", not enabled)
        text = _set_desktop_key(text, "X-GNOME-Autostart-enabled", "true" if enabled else "false")
        path.write_text(text, encoding="utf-8")
        return True, "Updated"
    except OSError as e:
        return False, str(e)


def _set_desktop_bool(text: str, key: str, value: bool) -> str:
    return _set_desktop_key(text, key, "true" if value else "false")


def _set_desktop_key(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    found = False
    in_entry = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_entry and not found:
                out.append(f"{key}={value}")
                found = True
            in_entry = stripped == "[Desktop Entry]"
            out.append(line)
            continue
        if in_entry and stripped.startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
            continue
        out.append(line)
    if in_entry and not found:
        out.append(f"{key}={value}")
    elif not found:
        # no Desktop Entry? append
        if not any(l.strip() == "[Desktop Entry]" for l in out):
            out.insert(0, "[Desktop Entry]")
        out.append(f"{key}={value}")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")
