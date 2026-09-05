"""systemd service listing and control."""

from __future__ import annotations

from dataclasses import dataclass, field

from .util import run, run_privileged, which


@dataclass
class ServiceRow:
    name: str
    load: str
    active: str
    sub: str
    description: str
    unit_file_state: str = ""  # enabled/disabled/static/...


@dataclass
class ServiceListResult:
    rows: list[ServiceRow] = field(default_factory=list)
    error: str | None = None


def list_services(*, user: bool = False, limit: int = 400) -> ServiceListResult:
    systemctl = which("systemctl")
    if not systemctl:
        return ServiceListResult(error="systemctl not found")

    base = [systemctl]
    if user:
        base.append("--user")

    rows: dict[str, ServiceRow] = {}
    errors: list[str] = []

    try:
        r = run(
            [*base, "list-units", "--type=service", "--all", "--no-pager", "--no-legend", "--plain"],
            timeout=30,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "list-units failed").strip()
            errors.append(err[:300])
        for line in (r.stdout or "").splitlines():
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            name, load, active, sub = parts[0], parts[1], parts[2], parts[3]
            desc = parts[4] if len(parts) > 4 else ""
            if not name.endswith(".service"):
                name = name + ".service" if "." not in name else name
            rows[name] = ServiceRow(name=name, load=load, active=active, sub=sub, description=desc)
    except Exception as e:  # noqa: BLE001
        errors.append(str(e)[:300])

    try:
        r = run(
            [
                *base,
                "list-unit-files",
                "--type=service",
                "--no-pager",
                "--no-legend",
                "--plain",
            ],
            timeout=30,
        )
        if r.returncode != 0 and not rows:
            err = (r.stderr or r.stdout or "list-unit-files failed").strip()
            errors.append(err[:300])
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            name, state = parts[0], parts[1]
            if name in rows:
                rows[name].unit_file_state = state
            else:
                rows[name] = ServiceRow(
                    name=name,
                    load="loaded" if state != "not-found" else "not-found",
                    active="inactive",
                    sub="dead",
                    description="",
                    unit_file_state=state,
                )
    except Exception as e:  # noqa: BLE001
        if not rows:
            errors.append(str(e)[:300])

    out = list(rows.values())
    out.sort(key=lambda s: (0 if s.active == "active" else 1, s.name.lower()))
    if not out and errors:
        return ServiceListResult(rows=[], error=errors[0])
    if out and errors:
        # partial success — show rows + soft warning
        return ServiceListResult(rows=out[:limit], error=f"(partial) {errors[0]}")
    return ServiceListResult(rows=out[:limit], error=None)


def service_action(name: str, action: str, *, user: bool = False) -> tuple[bool, str]:
    """action: start|stop|restart|enable|disable"""
    systemctl = which("systemctl")
    if not systemctl:
        return False, "systemctl not found"
    if not name.endswith(".service") and not name.endswith(".timer"):
        name = f"{name}.service"
    cmd = [systemctl]
    if user:
        cmd.append("--user")
    cmd.extend([action, name])
    try:
        if user:
            r = run(cmd, timeout=60)
        else:
            r = run_privileged(cmd, timeout=60)
        if r.returncode == 0:
            return True, f"{action} {name}: ok"
        err = (r.stderr or r.stdout or "failed").strip()
        return False, err[:500]
    except Exception as e:  # noqa: BLE001
        return False, str(e)
