"""Pacman, Omarchy, AUR and Flatpak update discovery and execution."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .util import run, run_privileged, which


@dataclass(frozen=True)
class UpdateItem:
    manager: str
    package: str
    current: str
    available: str
    architecture: str = ""


@dataclass(frozen=True)
class UpdateResult:
    ok: bool
    message: str


def list_updates() -> tuple[list[UpdateItem], list[str]]:
    items: list[UpdateItem] = []
    errors: list[str] = []
    scanners = [_list_pacman_updates, _list_omarchy_update]
    if which("yay") or which("paru"):
        scanners.append(_list_aur_updates)
    if which("flatpak"):
        scanners.append(_list_flatpak_updates)
    with ThreadPoolExecutor(max_workers=max(1, len(scanners))) as executor:
        futures = [executor.submit(scanner) for scanner in scanners]
        for future in as_completed(futures):
            found, warnings = future.result()
            items.extend(found)
            errors.extend(warnings)

    items.sort(key=lambda item: (item.manager, item.package.lower()))
    return items, errors


def _list_pacman_updates() -> tuple[list[UpdateItem], list[str]]:
    checkupdates = which("checkupdates")
    if not checkupdates:
        return [], ["Pacman: install pacman-contrib for checkupdates"]
    try:
        result = run([checkupdates, "--nocolor"], timeout=90)
        # 0 = updates listed, 2 = none, anything else is an error.
        if result.returncode not in (0, 2):
            err = (result.stderr or result.stdout or "checkupdates failed").strip()[:200]
            return [], [f"Pacman: {err}"]
        items: list[UpdateItem] = []
        for line in (result.stdout or "").splitlines():
            match = re.match(r"^(\S+)\s+(\S+)\s+->\s+(\S+)$", line.strip())
            if match:
                package, current, available = match.groups()
                items.append(UpdateItem("Pacman", package, current, available))
        return items, []
    except Exception as exc:  # noqa: BLE001
        return [], [f"Pacman: {exc}"]


def _list_omarchy_update() -> tuple[list[UpdateItem], list[str]]:
    omarchy = which("omarchy")
    if not omarchy:
        return [], []
    try:
        result = run([omarchy, "update", "available"], timeout=30)
        text = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        if result.returncode != 0:
            return [], []
        lower = text.lower()
        if "up to date" in lower or "no update" in lower:
            return [], []
        if "available" in lower or "update" in lower:
            summary = text.splitlines()[0][:80] if text else "available"
            return [UpdateItem("Omarchy", "omarchy", "installed", summary)], []
        return [], []
    except Exception as exc:  # noqa: BLE001
        return [], [f"Omarchy: {exc}"]


def _list_aur_updates() -> tuple[list[UpdateItem], list[str]]:
    helper = which("yay") or which("paru")
    if not helper:
        return [], []
    try:
        result = run([helper, "-Qua"], timeout=90)
        items: list[UpdateItem] = []
        for line in (result.stdout or "").splitlines():
            match = re.match(r"^(\S+)\s+(\S+)\s+->\s+(\S+)$", line.strip())
            if match:
                package, current, available = match.groups()
                items.append(UpdateItem("AUR", package, current, available))
                continue
            cols = line.split()
            if len(cols) >= 2:
                items.append(UpdateItem("AUR", cols[0], cols[1], cols[-1] if len(cols) > 2 else "available"))
        errors = [] if result.returncode in (0, 1) else [(result.stderr or "AUR query failed").strip()[:200]]
        return items, errors
    except Exception as exc:  # noqa: BLE001
        return [], [f"AUR: {exc}"]


def _list_flatpak_updates() -> tuple[list[UpdateItem], list[str]]:
    try:
        result = run(["flatpak", "remote-ls", "--updates", "--columns=application,version"], timeout=30)
        items = []
        for line in (result.stdout or "").splitlines():
            cols = line.split("\t", 1)
            if cols and cols[0]:
                items.append(UpdateItem("Flatpak", cols[0], "installed", cols[1] if len(cols) > 1 else "available"))
        errors = [] if result.returncode == 0 else [(result.stderr or "flatpak update check failed").strip()[:200]]
        return items, errors
    except Exception as exc:  # noqa: BLE001
        return [], [f"Flatpak: {exc}"]


def refresh_metadata() -> UpdateResult:
    pacman = which("pacman")
    if not pacman:
        return UpdateResult(False, "pacman not found")
    try:
        result = run_privileged([pacman, "-Sy"], timeout=900)
    except Exception as exc:  # noqa: BLE001
        return UpdateResult(False, str(exc))
    output = (result.stderr or result.stdout or "").strip()
    return UpdateResult(result.returncode == 0, output[-1000:] or "Pacman databases synced")


def install_updates(items: list[UpdateItem]) -> UpdateResult:
    if not items:
        return UpdateResult(False, "No updates selected")
    by_manager: dict[str, list[str]] = {}
    for item in items:
        by_manager.setdefault(item.manager, []).append(item.package)
    messages: list[str] = []
    ok = True
    try:
        pacman_packages = by_manager.get("Pacman", [])
        if pacman_packages:
            pacman = which("pacman") or "pacman"
            result = run_privileged(
                [pacman, "-S", "--noconfirm", "--needed", "--", *pacman_packages],
                timeout=3600,
            )
            ok = ok and result.returncode == 0
            messages.append((result.stderr or result.stdout or "Pacman update finished").strip()[-1200:])
        if by_manager.get("Omarchy"):
            omarchy = which("omarchy")
            if not omarchy:
                ok = False
                messages.append("omarchy not found")
            else:
                result = run([omarchy, "update", "-y"], timeout=3600)
                ok = ok and result.returncode == 0
                messages.append((result.stderr or result.stdout or "Omarchy update finished").strip()[-1200:])
        aur_packages = by_manager.get("AUR", [])
        if aur_packages:
            helper = which("yay") or which("paru")
            if not helper:
                ok = False
                messages.append("No AUR helper (yay/paru) found")
            else:
                result = run([helper, "-S", "--noconfirm", "--", *aur_packages], timeout=3600)
                ok = ok and result.returncode == 0
                messages.append((result.stderr or result.stdout or "AUR update finished").strip()[-1200:])
        flatpak_packages = by_manager.get("Flatpak", [])
        if flatpak_packages:
            result = run(["flatpak", "update", "-y", *flatpak_packages], timeout=3600)
            ok = ok and result.returncode == 0
            messages.append((result.stderr or result.stdout or "Flatpak update finished").strip()[-1200:])
    except Exception as exc:  # noqa: BLE001
        return UpdateResult(False, str(exc))
    return UpdateResult(ok, "\n".join(filter(None, messages))[-3000:] or "Updates finished")
