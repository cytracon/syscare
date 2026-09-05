"""List and remove installed pacman packages."""

from __future__ import annotations

from dataclasses import dataclass

from .util import human_bytes, run, run_privileged, which


@dataclass
class PackageRow:
    name: str
    version: str
    arch: str
    size: int
    description: str
    automatic: bool = False


@dataclass
class RemovePlan:
    ok: bool
    action: str
    names: list[str]
    summary: str
    raw: str
    error: str = ""


def list_packages(limit: int = 5000) -> list[PackageRow]:
    rows: list[PackageRow] = []
    deps = _pacman_deps()
    expac = which("expac")
    if expac:
        try:
            r = run([expac, "-Q", "%n\t%v\t%a\t%m\t%d"], timeout=60)
        except Exception:  # noqa: BLE001
            r = None
        if r and r.returncode == 0:
            for line in (r.stdout or "").splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                name, version = parts[0], parts[1]
                arch = parts[2] if len(parts) > 2 else ""
                try:
                    size = int(parts[3]) if len(parts) > 3 and parts[3] else 0
                except ValueError:
                    size = 0
                summary = parts[4] if len(parts) > 4 else ""
                rows.append(
                    PackageRow(
                        name=name,
                        version=version,
                        arch=arch,
                        size=size,
                        description=summary,
                        automatic=name in deps,
                    )
                )
            rows.sort(key=lambda p: p.name.lower())
            return rows[:limit]

    try:
        r = run(["pacman", "-Q"], timeout=60)
    except Exception:  # noqa: BLE001
        return rows
    if r.returncode != 0:
        return rows
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, version = parts[0], parts[1]
        rows.append(
            PackageRow(
                name=name,
                version=version,
                arch="",
                size=0,
                description="",
                automatic=name in deps,
            )
        )
    rows.sort(key=lambda p: p.name.lower())
    return rows[:limit]


def _pacman_deps() -> set[str]:
    try:
        r = run(["pacman", "-Qqd"], timeout=30)
        return {ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()}
    except Exception:  # noqa: BLE001
        return set()


def simulate_remove(names: list[str], *, purge: bool = False) -> RemovePlan:
    if not names:
        return RemovePlan(False, "remove", [], "", "", "No packages selected")
    pacman = which("pacman")
    if not pacman:
        return RemovePlan(False, "remove", names, "", "", "pacman not found")
    action = "remove-nosave" if purge else "remove"
    flags = ["-R", "--print", "--print-format", "%n %v"]
    if purge:
        flags = ["-Rns", "--print", "--print-format", "%n %v"]
    cmd = [pacman, *flags, "--", *names]
    try:
        r = run(cmd, timeout=120)
    except Exception as e:  # noqa: BLE001
        return RemovePlan(False, action, names, "", "", str(e))

    raw = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if r.returncode != 0:
        return RemovePlan(
            False,
            action,
            names,
            "",
            raw[:4000],
            (r.stderr or r.stdout or "simulate failed")[:500],
        )

    interesting = [line.strip() for line in raw.splitlines() if line.strip()]
    summary = "\n".join(interesting[:80]) if interesting else raw[:1500]
    return RemovePlan(True, action, names, summary, raw[:8000], "")


def remove_packages(names: list[str], *, purge: bool = False) -> tuple[bool, str]:
    if not names:
        return False, "No packages selected"
    pacman = which("pacman")
    if not pacman:
        return False, "pacman not found"
    action = "remove-nosave" if purge else "remove"
    flags = ["-Rns", "--noconfirm"] if purge else ["-R", "--noconfirm"]
    try:
        r = run_privileged([pacman, *flags, "--", *names], timeout=600)
        if r.returncode == 0:
            return True, f"{action} ok: {', '.join(names[:8])}{'…' if len(names) > 8 else ''}"
        return False, (r.stderr or r.stdout or "remove failed")[:800]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def format_pkg_size(p: PackageRow) -> str:
    return human_bytes(p.size)
