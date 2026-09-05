"""System information for Dashboard."""

from __future__ import annotations

import os
import platform
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from .util import human_bytes, read_text


@dataclass
class DiskInfo:
    device: str
    mount: str
    fstype: str
    total: int
    used: int
    free: int
    percent: float


@dataclass
class SystemSnapshot:
    hostname: str = ""
    os_pretty: str = ""
    kernel: str = ""
    arch: str = ""
    uptime_sec: float = 0.0
    boot_time: float = 0.0
    cpu_model: str = ""
    cpu_count_logical: int = 0
    cpu_count_physical: int = 0
    cpu_freq_mhz: float | None = None
    cpu_percent: float = 0.0
    load_avg: tuple[float, float, float] = (0.0, 0.0, 0.0)
    mem_total: int = 0
    mem_available: int = 0
    mem_used: int = 0
    mem_percent: float = 0.0
    swap_total: int = 0
    swap_used: int = 0
    swap_percent: float = 0.0
    disks: list[DiskInfo] = field(default_factory=list)
    net_bytes_sent: int = 0
    net_bytes_recv: int = 0
    process_count: int = 0
    desktop: str = ""
    username: str = ""


def _os_pretty() -> str:
    p = Path("/etc/os-release")
    data: dict[str, str] = {}
    for line in read_text(p).splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            data[k] = v.strip().strip('"')
    return data.get("PRETTY_NAME") or f"{platform.system()} {platform.release()}"


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name") or line.lower().startswith("hardware"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "CPU"


def format_uptime(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    if not days and not hours:
        parts.append(f"{secs}s")
    return " ".join(parts)


def collect(cpu_interval: float = 0.15) -> SystemSnapshot:
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    boot = psutil.boot_time()
    uptime = time.time() - boot
    try:
        load = os.getloadavg()
    except OSError:
        load = (0.0, 0.0, 0.0)
    freq = None
    try:
        f = psutil.cpu_freq()
        if f:
            freq = f.current
    except Exception:  # noqa: BLE001
        pass

    disks: list[DiskInfo] = []
    seen: set[str] = set()
    for p in psutil.disk_partitions(all=False):
        if p.mountpoint in seen:
            continue
        if p.fstype in ("", "squashfs", "tmpfs", "devtmpfs", "overlay", "efivarfs"):
            continue
        if p.mountpoint.startswith(("/snap", "/boot/efi", "/var/snap", "/run", "/boot")):
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        seen.add(p.mountpoint)
        disks.append(
            DiskInfo(
                device=p.device,
                mount=p.mountpoint,
                fstype=p.fstype,
                total=u.total,
                used=u.used,
                free=u.free,
                percent=u.percent,
            )
        )
    disks.sort(key=lambda d: d.mount)

    net = psutil.net_io_counters()
    try:
        cpu_pct = psutil.cpu_percent(interval=cpu_interval)
    except Exception:  # noqa: BLE001
        cpu_pct = 0.0

    return SystemSnapshot(
        hostname=socket.gethostname(),
        os_pretty=_os_pretty(),
        kernel=platform.release(),
        arch=platform.machine(),
        uptime_sec=uptime,
        boot_time=boot,
        cpu_model=_cpu_model(),
        cpu_count_logical=psutil.cpu_count(logical=True) or 0,
        cpu_count_physical=psutil.cpu_count(logical=False) or 0,
        cpu_freq_mhz=freq,
        cpu_percent=cpu_pct,
        load_avg=(float(load[0]), float(load[1]), float(load[2])),
        mem_total=mem.total,
        mem_available=mem.available,
        mem_used=mem.used,
        mem_percent=mem.percent,
        swap_total=swap.total,
        swap_used=swap.used,
        swap_percent=swap.percent,
        disks=disks,
        net_bytes_sent=getattr(net, "bytes_sent", 0) or 0,
        net_bytes_recv=getattr(net, "bytes_recv", 0) or 0,
        process_count=len(psutil.pids()),
        desktop=os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("DESKTOP_SESSION", ""),
        username=os.environ.get("USER") or os.environ.get("LOGNAME") or "",
    )


def status_payload() -> dict:
    """Compact JSON for the Omarchy bar plugin (no extra package queries)."""
    from . import __version__

    snap = collect(cpu_interval=0.08)
    root_disk = next((d for d in snap.disks if d.mount == "/"), snap.disks[0] if snap.disks else None)
    return {
        "version": __version__,
        "hostname": snap.hostname,
        "os": snap.os_pretty,
        "kernel": snap.kernel,
        "desktop": snap.desktop,
        "uptime_sec": int(snap.uptime_sec),
        "uptime": format_uptime(snap.uptime_sec),
        "cpu_percent": round(snap.cpu_percent, 1),
        "mem_percent": round(snap.mem_percent, 1),
        "mem_used": snap.mem_used,
        "mem_total": snap.mem_total,
        "disk_percent": round(root_disk.percent, 1) if root_disk else 0.0,
        "disk_used": root_disk.used if root_disk else 0,
        "disk_total": root_disk.total if root_disk else 0,
        "process_count": snap.process_count,
    }


def summary_lines(s: SystemSnapshot) -> list[tuple[str, str]]:
    freq = f"{s.cpu_freq_mhz:.0f} MHz" if s.cpu_freq_mhz else "—"
    return [
        ("Hostname", s.hostname),
        ("User", s.username),
        ("OS", s.os_pretty),
        ("Kernel", s.kernel),
        ("Arch", s.arch),
        ("Desktop", s.desktop or "—"),
        ("Uptime", format_uptime(s.uptime_sec)),
        ("CPU", s.cpu_model),
        ("Cores", f"{s.cpu_count_physical} physical / {s.cpu_count_logical} logical @ {freq}"),
        ("CPU usage", f"{s.cpu_percent:.0f}%"),
        ("Load avg", f"{s.load_avg[0]:.2f} / {s.load_avg[1]:.2f} / {s.load_avg[2]:.2f}"),
        ("Memory", f"{human_bytes(s.mem_used)} / {human_bytes(s.mem_total)} ({s.mem_percent:.0f}%)"),
        ("Swap", f"{human_bytes(s.swap_used)} / {human_bytes(s.swap_total)} ({s.swap_percent:.0f}%)"),
        ("Processes", str(s.process_count)),
        ("Network ↓/↑", f"{human_bytes(s.net_bytes_recv)} / {human_bytes(s.net_bytes_sent)}"),
    ]
