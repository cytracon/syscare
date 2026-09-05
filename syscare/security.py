"""Linux security posture audit and optional ClamAV quarantine workflow."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .history import state_dir
from .util import read_text, run, run_privileged, which


SYSCTL_HARDENING = {
    "aslr": ("kernel.randomize_va_space", "2", "Full address-space randomization"),
    "kernel-pointers": ("kernel.kptr_restrict", "2", "Hide kernel pointers"),
    "dmesg": ("kernel.dmesg_restrict", "1", "Restrict kernel log access"),
    "ptrace": ("kernel.yama.ptrace_scope", "1", "Restrict process tracing"),
    "unprivileged-bpf": ("kernel.unprivileged_bpf_disabled", "1", "Disable unprivileged BPF"),
    "syn-cookies": ("net.ipv4.tcp_syncookies", "1", "Enable TCP SYN cookies"),
    "icmp-broadcast": ("net.ipv4.icmp_echo_ignore_broadcasts", "1", "Ignore ICMP broadcasts"),
    "reverse-path": ("net.ipv4.conf.all.rp_filter", "1", "Enable reverse-path filtering"),
    "ipv4-redirects": ("net.ipv4.conf.all.accept_redirects", "0", "Reject IPv4 redirects"),
    "source-route": ("net.ipv4.conf.all.accept_source_route", "0", "Reject source-routed packets"),
    "martians": ("net.ipv4.conf.all.log_martians", "1", "Log impossible source addresses"),
    "ipv6-redirects": ("net.ipv6.conf.all.accept_redirects", "0", "Reject IPv6 redirects"),
}


@dataclass(frozen=True)
class SecurityCheck:
    id: str
    title: str
    status: str  # pass | warning | info
    detail: str


@dataclass(frozen=True)
class MalwareFinding:
    path: Path
    threat: str


def audit() -> list[SecurityCheck]:
    checks = [
        _firewall_check(),
        _automatic_updates_check(),
        _disk_encryption_check(),
        _secure_boot_check(),
        _mac_check(),
        _screen_lock_check(),
        _ssh_check(),
        _fail2ban_check(),
        _listening_ports_check(),
        _clamav_check(),
        _polkit_check(),
    ]
    checks.extend(_sysctl_checks())
    return checks


def hardening_ids() -> set[str]:
    return {f"sysctl-{key}" for key in SYSCTL_HARDENING}


def apply_hardening(ids: list[str]) -> tuple[int, list[str]]:
    allowed = hardening_ids()
    requested = [item for item in dict.fromkeys(ids) if item in allowed]
    applied = 0
    errors: list[str] = []

    sysctl_ids = [item.removeprefix("sysctl-") for item in requested if item.startswith("sysctl-")]
    if sysctl_ids:
        existing = read_text(Path("/etc/sysctl.d/99-syscare-hardening.conf"))
        values: dict[str, str] = {}
        for line in existing.splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, value = line.split("=", 1)
                values[name.strip()] = value.strip()
        for item in sysctl_ids:
            name, value, _label = SYSCTL_HARDENING[item]
            values[name] = value
        content = "# Managed by SysCare. Remove this file and run sysctl --system to revert.\n"
        content += "".join(f"{name} = {value}\n" for name, value in sorted(values.items()))
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", prefix="syscare-hardening-", delete=False) as handle:
                handle.write(content)
                temp_path = Path(handle.name)
            os.chmod(temp_path, 0o644)
            install = which("install") or "/usr/bin/install"
            result = run_privileged(
                [install, "-m", "0644", str(temp_path), "/etc/sysctl.d/99-syscare-hardening.conf"],
                timeout=60,
            )
            if result.returncode == 0:
                sysctl = which("sysctl") or "/usr/sbin/sysctl"
                result = run_privileged([sysctl, "--system"], timeout=120)
            if result.returncode == 0:
                applied += len(sysctl_ids)
            else:
                errors.append((result.stderr or result.stdout or "sysctl hardening failed").strip()[-500:])
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
    return applied, errors


def quick_malware_scan(root: Path | None = None) -> tuple[list[MalwareFinding], str]:
    clamscan = which("clamscan")
    if not clamscan:
        return [], "ClamAV is not installed (optional package: clamav)"
    scan_root = (root or (Path.home() / "Downloads")).expanduser().resolve()
    if not scan_root.is_dir():
        return [], f"Scan folder does not exist: {scan_root}"
    allowed = [Path.home().resolve(), Path("/tmp").resolve()]
    if not any(_is_relative_to(scan_root, base) for base in allowed):
        return [], "Quick scan is limited to the home directory and /tmp"
    result = run(
        [
            clamscan,
            "--recursive=yes",
            "--infected",
            "--no-summary",
            "--max-filesize=100M",
            "--max-scansize=2G",
            "--",
            str(scan_root),
        ],
        timeout=3600,
    )
    findings: list[MalwareFinding] = []
    for line in (result.stdout or "").splitlines():
        match = re.match(r"^(.*):\s+(.+)\s+FOUND$", line)
        if match:
            findings.append(MalwareFinding(Path(match.group(1)), match.group(2)))
    if findings:
        return findings, f"{len(findings)} threat(s) found"
    if result.returncode == 0:
        return [], "No threats found"
    return [], (result.stderr or result.stdout or f"ClamAV exited with {result.returncode}").strip()[-500:]


def quarantine(findings: list[MalwareFinding]) -> tuple[int, list[str]]:
    root = state_dir() / "quarantine"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    metadata_path = root / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        metadata = []
    moved = 0
    errors: list[str] = []
    for finding in findings:
        try:
            expanded = finding.path.expanduser()
            if expanded.is_symlink():
                raise ValueError("refusing to quarantine through a symlink")
            source = expanded.resolve()
            if not source.is_file() or source.is_symlink():
                raise ValueError("not a regular file")
            if not (_is_relative_to(source, Path.home().resolve()) or _is_relative_to(source, Path("/tmp").resolve())):
                raise ValueError("path is outside allowed scan roots")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            destination = root / f"{stamp}-{moved}-{source.name}"
            shutil.move(str(source), destination)
            os.chmod(destination, 0o600)
            metadata.append(
                {
                    "original": str(source),
                    "quarantined": str(destination),
                    "threat": finding.threat,
                    "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            )
            moved += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{finding.path}: {exc}")
    temp = metadata_path.with_suffix(".tmp")
    temp.write_text(json.dumps(metadata[-1000:], indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(metadata_path)
    return moved, errors


def _firewall_check() -> SecurityCheck:
    ufw = which("ufw")
    if ufw:
        result = run([ufw, "status"], timeout=15)
        text = f"{result.stdout}\n{result.stderr}".strip()
        active = bool(re.search(r"Status:\s+active", text, re.I))
        if "need to be root" in text.lower() and which("systemctl"):
            service = run([which("systemctl"), "is-active", "ufw"], timeout=15)
            active = service.returncode == 0 and (service.stdout or "").strip() == "active"
            text = "UFW service active (rules require administrator access)" if active else text
        return SecurityCheck("firewall", "Firewall", "pass" if active else "warning", text[:240] or "UFW status unavailable")
    nft = which("nft")
    if nft:
        result = run([nft, "list", "ruleset"], timeout=15)
        rules = (result.stdout or "").strip()
        if result.returncode == 0 and rules:
            return SecurityCheck("firewall", "Firewall", "pass", "nftables ruleset is present")
        if result.returncode != 0:
            return SecurityCheck("firewall", "Firewall", "info", "nftables present (listing may need privileges)")
    return SecurityCheck("firewall", "Firewall", "warning", "No UFW/nftables firewall detected")


def _automatic_updates_check() -> SecurityCheck:
    systemctl = which("systemctl")
    if systemctl:
        for unit in ("omarchy-update.timer", "pacman-filesdb-refresh.timer"):
            result = run([systemctl, "is-enabled", unit], timeout=10)
            if result.returncode == 0 and (result.stdout or "").strip() == "enabled":
                return SecurityCheck(
                    "automatic-updates",
                    "Automatic updates",
                    "pass",
                    f"{unit} is enabled",
                )
    return SecurityCheck(
        "automatic-updates",
        "Automatic updates",
        "info",
        "Omarchy uses manual `omarchy update`; unattended upgrades are not enabled",
    )


def _disk_encryption_check() -> SecurityCheck:
    lsblk = which("lsblk")
    if not lsblk:
        return SecurityCheck("encryption", "Disk encryption", "info", "lsblk unavailable")
    result = run([lsblk, "-J", "-o", "TYPE,FSTYPE,MOUNTPOINT"], timeout=20)
    encrypted = "crypto_LUKS" in (result.stdout or "") or '"type":"crypt"' in (result.stdout or "").replace(" ", "")
    return SecurityCheck("encryption", "Disk encryption", "pass" if encrypted else "warning", "LUKS encrypted volume detected" if encrypted else "No LUKS encrypted volume detected")


def _secure_boot_check() -> SecurityCheck:
    mokutil = which("mokutil")
    if not mokutil:
        return SecurityCheck("secure-boot", "Secure Boot", "info", "mokutil unavailable")
    result = run([mokutil, "--sb-state"], timeout=15)
    text = (result.stdout or result.stderr or "").strip()
    enabled = "enabled" in text.lower()
    return SecurityCheck("secure-boot", "Secure Boot", "pass" if enabled else "warning", text or "Unknown")


def _mac_check() -> SecurityCheck:
    aa_status = which("aa-status")
    if aa_status:
        result = run([aa_status, "--enabled"], timeout=15)
        if result.returncode == 0:
            return SecurityCheck("mac", "Mandatory access control", "pass", "AppArmor enabled")
    systemctl = which("systemctl")
    if systemctl:
        result = run([systemctl, "is-active", "apparmor"], timeout=15)
        if result.returncode == 0 and (result.stdout or "").strip() == "active":
            return SecurityCheck("mac", "Mandatory access control", "pass", "AppArmor active")
    return SecurityCheck(
        "mac",
        "Mandatory access control",
        "info",
        "Omarchy/Arch does not enable AppArmor by default",
    )


def _screen_lock_check() -> SecurityCheck:
    shell = Path.home() / ".config/omarchy/shell.json"
    try:
        data = json.loads(shell.read_text(encoding="utf-8"))
        lock = data.get("idle", {}).get("lock") if isinstance(data, dict) else None
        if isinstance(lock, (int, float)) and lock > 0:
            seconds = int(lock)
            return SecurityCheck(
                "screen-lock",
                "Screen lock",
                "pass",
                f"Omarchy locks after {seconds}s idle (hyprlock)",
            )
        if lock == 0:
            return SecurityCheck("screen-lock", "Screen lock", "warning", "Omarchy idle.lock is 0 (disabled)")
    except (OSError, ValueError, TypeError):
        pass
    if which("hyprlock"):
        return SecurityCheck("screen-lock", "Screen lock", "info", "hyprlock is installed; idle.lock not set in shell.json")
    return SecurityCheck("screen-lock", "Screen lock", "warning", "No Omarchy idle.lock or hyprlock detected")


def _polkit_check() -> SecurityCheck:
    if which("pkexec"):
        return SecurityCheck("polkit", "Privilege prompts", "pass", "pkexec is available for administrator actions")
    return SecurityCheck("polkit", "Privilege prompts", "warning", "pkexec not found — privileged SysCare actions will fail")


def _ssh_check() -> SecurityCheck:
    config = read_text(Path("/etc/ssh/sshd_config"))
    if not config:
        return SecurityCheck("ssh", "SSH hardening", "info", "OpenSSH server is not configured")
    root_login = re.findall(r"^\s*PermitRootLogin\s+(\S+)", config, re.I | re.M)
    password = re.findall(r"^\s*PasswordAuthentication\s+(\S+)", config, re.I | re.M)
    safe_root = not root_login or root_login[-1].lower() in {"no", "prohibit-password", "without-password"}
    detail = f"PermitRootLogin={root_login[-1] if root_login else 'default'}, PasswordAuthentication={password[-1] if password else 'default'}"
    return SecurityCheck("ssh", "SSH hardening", "pass" if safe_root else "warning", detail)


def _fail2ban_check() -> SecurityCheck:
    systemctl = which("systemctl")
    if not systemctl:
        return SecurityCheck("fail2ban", "Fail2ban", "info", "systemctl unavailable")
    result = run([systemctl, "is-active", "fail2ban"], timeout=15)
    active = result.returncode == 0
    return SecurityCheck("fail2ban", "Fail2ban", "pass" if active else "info", "Active" if active else "Not active / not installed")


def _listening_ports_check() -> SecurityCheck:
    ss = which("ss")
    if not ss:
        return SecurityCheck("ports", "Listening TCP ports", "info", "ss unavailable")
    result = run([ss, "-tlnH"], timeout=15)
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    return SecurityCheck("ports", "Listening TCP ports", "info", f"{len(lines)} listening TCP socket(s)")


def _clamav_check() -> SecurityCheck:
    clamscan = which("clamscan")
    if not clamscan:
        return SecurityCheck("clamav", "Malware engine", "info", "ClamAV is optional and not installed")
    result = run([clamscan, "--version"], timeout=15)
    return SecurityCheck("clamav", "Malware engine", "pass", (result.stdout or "ClamAV installed").strip())


def _sysctl_checks() -> list[SecurityCheck]:
    sysctl = which("sysctl")
    checks: list[SecurityCheck] = []
    for key, (name, expected, label) in SYSCTL_HARDENING.items():
        if not sysctl:
            checks.append(SecurityCheck(f"sysctl-{key}", label, "info", "sysctl unavailable"))
            continue
        result = run([sysctl, "-n", name], timeout=15)
        current = (result.stdout or "").strip()
        if result.returncode != 0:
            checks.append(SecurityCheck(f"sysctl-{key}", label, "info", "Kernel setting is unavailable"))
        else:
            checks.append(SecurityCheck(f"sysctl-{key}", label, "pass" if current == expected else "warning", f"{name}={current}; recommended {expected}"))
    return checks


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False
