"""Process listing and control."""

from __future__ import annotations

from dataclasses import dataclass

import psutil


@dataclass
class ProcRow:
    pid: int
    name: str
    user: str
    cpu: float
    mem: float
    mem_rss: int
    status: str
    cmdline: str


# Cache Process handles so cpu_percent() has a previous sample between refreshes.
_PROC_CACHE: dict[int, psutil.Process] = {}


def list_processes(limit: int = 500) -> list[ProcRow]:
    rows: list[ProcRow] = []
    live: dict[int, psutil.Process] = {}
    attrs = ["pid", "name", "username", "memory_percent", "memory_info", "status", "cmdline"]

    for p in psutil.process_iter(attrs):
        try:
            info = p.info
            pid = int(info.get("pid") or 0)
            if not pid:
                continue
            # reuse process object for meaningful cpu_percent deltas
            proc = _PROC_CACHE.get(pid)
            if proc is None or not proc.is_running():
                proc = p
                try:
                    proc.cpu_percent(None)  # prime first sample
                except (psutil.Error, ProcessLookupError):
                    pass
            live[pid] = proc
            try:
                cpu = float(proc.cpu_percent(None) or 0.0)
            except (psutil.Error, ProcessLookupError):
                cpu = 0.0
            mi = info.get("memory_info")
            rss = int(getattr(mi, "rss", 0) or 0)
            cmd = info.get("cmdline") or []
            cmdline = " ".join(cmd) if cmd else (info.get("name") or "")
            rows.append(
                ProcRow(
                    pid=pid,
                    name=str(info.get("name") or "?"),
                    user=str(info.get("username") or "?"),
                    cpu=cpu,
                    mem=float(info.get("memory_percent") or 0.0),
                    mem_rss=rss,
                    status=str(info.get("status") or ""),
                    cmdline=cmdline[:300],
                )
            )
        except (psutil.Error, ProcessLookupError, TypeError, ValueError):
            continue

    _PROC_CACHE.clear()
    _PROC_CACHE.update(live)
    rows.sort(key=lambda r: (r.cpu, r.mem), reverse=True)
    return rows[:limit]


def kill_process(pid: int, force: bool = False) -> tuple[bool, str]:
    try:
        p = psutil.Process(pid)
        if force:
            p.kill()
        else:
            p.terminate()
        return True, f"{'Killed' if force else 'Terminated'} PID {pid}"
    except psutil.AccessDenied:
        return False, f"Permission denied for PID {pid}"
    except psutil.NoSuchProcess:
        return False, f"No such process {pid}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)
