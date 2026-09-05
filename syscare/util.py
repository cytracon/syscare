"""Shared helpers: formatting, privileged exec, path utils."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Sequence


def human_bytes(n: float | int | None) -> str:
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if n < 0:
        n = 0.0
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    i = 0
    while n >= 1024.0 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        return f"{int(n)} {units[i]}"
    return f"{n:.1f} {units[i]}"


def human_rate(bps: float) -> str:
    return f"{human_bytes(bps)}/s"


def run(
    cmd: Sequence[str],
    *,
    timeout: float | None = 60,
    check: bool = False,
    text: bool = True,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd),
        capture_output=True,
        text=text,
        timeout=timeout,
        check=check,
        env=env,
    )


def which(name: str) -> str | None:
    return shutil.which(name)


def run_privileged(
    cmd: Sequence[str],
    *,
    timeout: float | None = 300,
) -> subprocess.CompletedProcess:
    """Run command as root via pkexec when not already root."""
    if os.geteuid() == 0:
        return run(cmd, timeout=timeout)
    pk = which("pkexec")
    if not pk:
        raise RuntimeError("pkexec not found — cannot elevate privileges")
    return run([pk, *cmd], timeout=timeout)


def dir_size(path: Path, *, max_entries: int = 200_000) -> int:
    """Best-effort recursive size; prefers `du` (fast), falls back to walk."""
    if not path.exists():
        return 0
    if path.is_file() or path.is_symlink():
        try:
            return path.stat().st_size
        except OSError:
            return 0

    # GNU du is much faster than Python os.walk on large trees
    du = which("du")
    if du:
        try:
            r = run([du, "-sb", "--apparent-size", str(path)], timeout=45)
            if r.returncode == 0 and r.stdout:
                # "12345\t/path"
                first = r.stdout.strip().split()[0]
                return max(0, int(first))
        except Exception:  # noqa: BLE001
            pass
        try:
            r = run([du, "-sb", str(path)], timeout=45)
            if r.returncode == 0 and r.stdout:
                first = r.stdout.strip().split()[0]
                return max(0, int(first))
        except Exception:  # noqa: BLE001
            pass

    total = 0
    count = 0
    try:
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in files:
                count += 1
                if count > max_entries:
                    return total
                fp = Path(root) / name
                try:
                    if fp.is_symlink():
                        continue
                    total += fp.stat().st_size
                except OSError:
                    continue
            if count > max_entries:
                break
    except OSError:
        pass
    return total


def safe_rm_tree(path: Path) -> tuple[bool, str]:
    """Remove a file or directory. Returns (ok, message)."""
    try:
        if not path.exists() and not path.is_symlink():
            return True, "already gone"
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
            return True, "removed"
        shutil.rmtree(path, ignore_errors=False)
        return True, "removed"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def empty_dir_contents(path: Path) -> tuple[int, list[str]]:
    """Delete children of path, keep the directory. Returns (bytes_freed_estimate, errors)."""
    errors: list[str] = []
    freed = 0
    if path.is_symlink():
        return 0, [f"refusing to follow symlink: {path}"]
    if not path.is_dir():
        return 0, [f"not a directory: {path}"]
    for child in list(path.iterdir()):
        try:
            if child.is_symlink() or child.is_file():
                try:
                    freed += child.stat().st_size
                except OSError:
                    pass
                child.unlink(missing_ok=True)
            elif child.is_dir():
                try:
                    freed += dir_size(child)
                except OSError:
                    pass
                shutil.rmtree(child, ignore_errors=False)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{child}: {e}")
    return freed, errors


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def xdg_cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default


def parse_env_file_desktop(path: Path) -> dict[str, str]:
    """Minimal .desktop key=value parser (no full desktop-entry semantics)."""
    data: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data
    in_entry = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_entry = line == "[Desktop Entry]"
            continue
        if not in_entry or "=" not in line:
            continue
        k, _, v = line.partition("=")
        data[k.strip()] = v.strip()
    return data


def iter_autostart_dirs() -> Iterable[Path]:
    yield xdg_config_home() / "autostart"
    yield Path("/etc/xdg/autostart")
