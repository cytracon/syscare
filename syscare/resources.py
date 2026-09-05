"""Live resource samples for the Resources page."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

import psutil


@dataclass
class ResourceSample:
    t: float
    cpu: float
    mem: float
    swap: float
    net_up: float  # bytes/s
    net_down: float
    disk_read: float  # bytes/s
    disk_write: float


@dataclass
class ResourceMonitor:
    history: deque[ResourceSample] = field(default_factory=lambda: deque(maxlen=120))
    _last_net: tuple[float, int, int] | None = None
    _last_disk: tuple[float, int, int] | None = None

    def sample(self) -> ResourceSample:
        now = time.time()
        cpu = float(psutil.cpu_percent(interval=None))
        mem = float(psutil.virtual_memory().percent)
        swap = float(psutil.swap_memory().percent)

        net_up = net_down = 0.0
        try:
            n = psutil.net_io_counters()
            sent, recv = int(n.bytes_sent), int(n.bytes_recv)
            if self._last_net is not None:
                lt, ls, lr = self._last_net
                dt = max(1e-6, now - lt)
                net_up = max(0.0, (sent - ls) / dt)
                net_down = max(0.0, (recv - lr) / dt)
            self._last_net = (now, sent, recv)
        except Exception:  # noqa: BLE001
            pass

        disk_r = disk_w = 0.0
        try:
            d = psutil.disk_io_counters()
            if d:
                rb, wb = int(d.read_bytes), int(d.write_bytes)
                if self._last_disk is not None:
                    lt, lr, lw = self._last_disk
                    dt = max(1e-6, now - lt)
                    disk_r = max(0.0, (rb - lr) / dt)
                    disk_w = max(0.0, (wb - lw) / dt)
                self._last_disk = (now, rb, wb)
        except Exception:  # noqa: BLE001
            pass

        s = ResourceSample(now, cpu, mem, swap, net_up, net_down, disk_r, disk_w)
        self.history.append(s)
        return s
