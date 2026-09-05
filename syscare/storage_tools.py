"""Disk analysis, duplicate/large/empty-file discovery, S.M.A.R.T. and TRIM."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .cleaner import _expand_path, _load_rule
from .util import dir_size, run, run_privileged, which


@dataclass(frozen=True)
class DiskRecord:
    device: str
    mountpoint: str
    filesystem: str
    size: int
    free: int
    health: str
    rotational: bool | None


@dataclass(frozen=True)
class FileRecord:
    path: Path
    size: int


@dataclass(frozen=True)
class DuplicateGroup:
    digest: str
    size: int
    paths: tuple[Path, ...]

    @property
    def reclaimable(self) -> int:
        return self.size * max(0, len(self.paths) - 1)


@dataclass(frozen=True)
class DatabaseRecord:
    application: str
    path: Path
    size: int


def disk_inventory() -> list[DiskRecord]:
    lsblk = which("lsblk")
    if not lsblk:
        return []
    result = run(
        [lsblk, "-J", "-b", "-o", "NAME,PATH,TYPE,FSTYPE,SIZE,MOUNTPOINT,ROTA"],
        timeout=30,
    )
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        return []
    records: list[DiskRecord] = []

    def walk(devices: list[dict]) -> None:
        for device in devices:
            mount = device.get("mountpoint")
            dtype = str(device.get("type") or "")
            if mount and dtype in {"part", "crypt", "lvm", "disk"}:
                try:
                    usage = shutil.disk_usage(str(mount))
                    free = usage.free
                except OSError:
                    free = 0
                path = str(device.get("path") or f"/dev/{device.get('name', '')}")
                records.append(
                    DiskRecord(
                        device=path,
                        mountpoint=str(mount),
                        filesystem=str(device.get("fstype") or ""),
                        size=int(device.get("size") or 0),
                        free=free,
                        health=_smart_health(_parent_disk(path)),
                        rotational=_bool_or_none(device.get("rota")),
                    )
                )
            children = device.get("children")
            if isinstance(children, list):
                walk(children)

    walk(data.get("blockdevices", []))
    unique: dict[str, DiskRecord] = {record.mountpoint: record for record in records}
    return sorted(unique.values(), key=lambda record: record.mountpoint)


def analyze_directory(root: Path, limit: int = 100) -> list[FileRecord]:
    root = _validated_scan_root(root)
    rows: list[FileRecord] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    for child in children:
        if child.is_symlink():
            continue
        rows.append(FileRecord(child, dir_size(child, max_entries=500_000)))
    rows.sort(key=lambda item: item.size, reverse=True)
    return rows[: max(1, limit)]


def find_large_files(root: Path, min_size: int = 100 * 1024**2, limit: int = 500) -> list[FileRecord]:
    root = _validated_scan_root(root)
    rows: list[FileRecord] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        for name in files:
            path = Path(current) / name
            try:
                if path.is_symlink():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            if size >= min_size:
                rows.append(FileRecord(path, size))
    rows.sort(key=lambda item: item.size, reverse=True)
    return rows[: max(1, limit)]


def find_duplicates(
    root: Path,
    min_size: int = 1024**2,
    max_files: int = 200_000,
) -> list[DuplicateGroup]:
    root = _validated_scan_root(root)
    by_size: dict[int, list[Path]] = {}
    seen = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        for name in files:
            seen += 1
            if seen > max_files:
                break
            path = Path(current) / name
            try:
                if path.is_symlink():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            if size >= min_size:
                by_size.setdefault(size, []).append(path)
        if seen > max_files:
            break

    groups: list[DuplicateGroup] = []
    for size, candidates in by_size.items():
        if len(candidates) < 2:
            continue
        by_quick: dict[str, list[Path]] = {}
        for path in candidates:
            digest = _hash_file(path, sample_only=True)
            if digest:
                by_quick.setdefault(digest, []).append(path)
        for quick_group in by_quick.values():
            if len(quick_group) < 2:
                continue
            by_full: dict[str, list[Path]] = {}
            for path in quick_group:
                digest = _hash_file(path, sample_only=False)
                if digest:
                    by_full.setdefault(digest, []).append(path)
            for digest, paths in by_full.items():
                if len(paths) > 1:
                    groups.append(DuplicateGroup(digest, size, tuple(sorted(paths))))
    groups.sort(key=lambda item: item.reclaimable, reverse=True)
    return groups


def find_empty_folders(root: Path, limit: int = 1000) -> list[Path]:
    root = _validated_scan_root(root)
    result: list[Path] = []
    for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
        path = Path(current)
        if path == root or path.is_symlink():
            continue
        try:
            if not any(path.iterdir()):
                result.append(path)
        except OSError:
            continue
        if len(result) >= limit:
            break
    return sorted(result)


def database_candidates() -> list[DatabaseRecord]:
    data = _load_rule("databases.json")
    shared = data.get("sharedDbFileSets", {})
    records: list[DatabaseRecord] = []
    seen: set[Path] = set()
    for target in data.get("targets", []):
        label = str(target.get("label") or "Application")
        base = _expand_path(str(target.get("basePath") or ""))
        db_files = target.get("dbFiles", [])
        if isinstance(db_files, str) and db_files.startswith("$"):
            db_files = shared.get(db_files[1:], [])
        if not isinstance(db_files, list) or not base.is_dir():
            continue
        roots = [base]
        if target.get("multiProfile"):
            roots = []
            patterns = target.get("profilePattern") or ["Default", "Profile *"]
            for pattern in patterns:
                roots.extend(path for path in base.glob(str(pattern)) if path.is_dir())
        for profile in roots:
            for relative in db_files:
                path = profile / str(relative)
                if path in seen or not path.is_file() or path.is_symlink():
                    continue
                try:
                    with path.open("rb") as handle:
                        if handle.read(16) != b"SQLite format 3\x00":
                            continue
                    size = path.stat().st_size
                except OSError:
                    continue
                seen.add(path)
                records.append(DatabaseRecord(label, path, size))
    return sorted(records, key=lambda item: (item.application.lower(), str(item.path)))


def optimize_databases(records: list[DatabaseRecord]) -> tuple[int, int, list[str]]:
    optimized = 0
    reclaimed = 0
    errors: list[str] = []
    for record in records:
        try:
            path = record.path.resolve()
            path.relative_to(Path.home().resolve())
            before = path.stat().st_size
            with sqlite3.connect(path, timeout=2) as connection:
                check = connection.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise RuntimeError(f"integrity check: {check[0] if check else 'failed'}")
                connection.execute("VACUUM")
            reclaimed += max(0, before - path.stat().st_size)
            optimized += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{record.path}: {exc}")
    return optimized, reclaimed, errors


def delete_user_paths(paths: list[Path], *, empty_dirs_only: bool = False) -> tuple[int, list[str]]:
    removed = 0
    errors: list[str] = []
    for path in paths:
        try:
            safe = _validated_delete_path(path)
            if empty_dirs_only and (not safe.is_dir() or any(safe.iterdir())):
                raise ValueError("folder is not empty")
            if safe.is_dir() and not safe.is_symlink():
                safe.rmdir() if empty_dirs_only else shutil.rmtree(safe)
            else:
                safe.unlink()
            removed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path}: {exc}")
    return removed, errors


def shred_file(path: Path, passes: int = 3) -> tuple[bool, str]:
    try:
        safe = _validated_delete_path(path)
    except ValueError as exc:
        return False, str(exc)
    if not safe.is_file() or safe.is_symlink():
        return False, "Only regular files can be shredded"
    shred = which("shred")
    if not shred:
        return False, "GNU shred not found"
    result = run([shred, "-u", "-z", "-n", str(max(1, min(passes, 10))), "--", str(safe)], timeout=3600)
    return result.returncode == 0, (result.stderr or result.stdout or "File shredded").strip()


def trim_mount(mountpoint: str) -> tuple[bool, str]:
    if not mountpoint.startswith("/") or "\n" in mountpoint or "\0" in mountpoint:
        return False, "Invalid mountpoint"
    fstrim = which("fstrim")
    if not fstrim:
        return False, "fstrim not found"
    try:
        result = run_privileged([fstrim, "-v", mountpoint], timeout=600)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return result.returncode == 0, (result.stderr or result.stdout or "TRIM finished").strip()


def _validated_scan_root(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"Not a directory: {resolved}")
    return resolved


def _validated_delete_path(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ValueError("refusing to delete through a symlink")
    resolved = expanded.resolve()
    home = Path.home().resolve()
    protected = {Path("/"), home, home / "Documents", home / "Downloads", home / "Pictures"}
    if resolved in protected or resolved == resolved.parent:
        raise ValueError("protected path")
    try:
        resolved.relative_to(home)
    except ValueError as exc:
        raise ValueError("SysCare only deletes user-home paths") from exc
    if not resolved.exists() and not resolved.is_symlink():
        raise ValueError("path no longer exists")
    return resolved


def _hash_file(path: Path, *, sample_only: bool) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            if sample_only:
                digest.update(handle.read(128 * 1024))
            else:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _smart_health(device: str) -> str:
    smartctl = which("smartctl")
    if not smartctl or not device:
        return "unknown"
    result = run([smartctl, "-H", device], timeout=20)
    text = f"{result.stdout}\n{result.stderr}".lower()
    if "passed" in text or "ok" in text:
        return "passed"
    if "failed" in text:
        return "failed"
    return "unknown"


def _parent_disk(device: str) -> str:
    # /dev/nvme0n1p3 -> /dev/nvme0n1, /dev/sda2 -> /dev/sda
    if "nvme" in device:
        return device.rsplit("p", 1)[0] if "p" in device.rsplit("/", 1)[-1] else device
    return device.rstrip("0123456789") or device


def _bool_or_none(value: object) -> bool | None:
    if value in (True, 1, "1"):
        return True
    if value in (False, 0, "0"):
        return False
    return None
