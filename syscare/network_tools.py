"""Network inspection and cache/profile maintenance for Linux."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .util import run, run_privileged, which


@dataclass(frozen=True)
class Connection:
    protocol: str
    local: str
    remote: str
    process: str


def established_connections(limit: int = 500) -> list[Connection]:
    ss = which("ss")
    if not ss:
        return []
    result = run([ss, "-tunap", "state", "established"], timeout=20)
    rows: list[Connection] = []
    for line in (result.stdout or "").splitlines():
        text = line.strip()
        if not text or text.startswith(("Netid", "State")):
            continue
        cols = text.split()
        addresses = [value for value in cols if ":" in value and not value.startswith("users:")]
        if len(addresses) < 2:
            continue
        process = next((value for value in cols if value.startswith("users:")), "")
        rows.append(Connection(cols[0], addresses[0], addresses[1], process))
        if len(rows) >= limit:
            break
    return rows


def wifi_profiles() -> list[str]:
    nmcli = which("nmcli")
    if not nmcli:
        return []
    result = run([nmcli, "-t", "-f", "NAME,TYPE", "connection", "show"], timeout=20)
    profiles: list[str] = []
    for line in (result.stdout or "").splitlines():
        name, sep, connection_type = line.rpartition(":")
        if sep and connection_type in {"802-11-wireless", "wifi"}:
            profiles.append(name.replace("\\:", ":"))
    return sorted(set(profiles), key=str.lower)


def delete_wifi_profile(name: str) -> tuple[bool, str]:
    if not name or "\n" in name or "\0" in name:
        return False, "Invalid Wi-Fi profile"
    nmcli = which("nmcli")
    if not nmcli:
        return False, "nmcli not found"
    result = run([nmcli, "connection", "delete", name], timeout=30)
    return result.returncode == 0, (result.stderr or result.stdout or "Profile deleted").strip()


def flush_dns() -> tuple[bool, str]:
    commands = []
    if which("resolvectl"):
        commands.append([which("resolvectl"), "flush-caches"])
    if which("systemd-resolve"):
        commands.append([which("systemd-resolve"), "--flush-caches"])
    if which("nscd"):
        commands.append([which("nscd"), "-i", "hosts"])
    for command in commands:
        result = run(command, timeout=20)
        if result.returncode == 0:
            return True, "DNS cache flushed"
    return False, "No supported DNS cache service found"


def clear_arp() -> tuple[bool, str]:
    ip = which("ip")
    if not ip:
        return False, "ip command not found"
    try:
        result = run_privileged([ip, "neigh", "flush", "all"], timeout=30)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    message = re.sub(r"\s+", " ", (result.stderr or result.stdout or "ARP cache cleared")).strip()
    return result.returncode == 0, message

