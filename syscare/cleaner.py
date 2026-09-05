"""System cleaner targets (scan + clean)."""

from __future__ import annotations

import logging
import os
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .util import (
    dir_size,
    empty_dir_contents,
    human_bytes,
    run,
    run_privileged,
    safe_rm_tree,
    which,
    xdg_cache_home,
    xdg_config_home,
    xdg_data_home,
)

log = logging.getLogger("syscare.cleaner")


@dataclass
class CleanTarget:
    id: str
    title: str
    description: str
    size: int = 0
    needs_root: bool = False
    selected: bool = True
    paths: list[Path] = field(default_factory=list)
    kind: str = "path"  # path | trash | apt | pkgcache | journal | residual | orphans
    category: str = "System"
    risk: str = "safe"  # safe | moderate | advanced


def _trash_dirs() -> list[Path]:
    base = xdg_data_home() / "Trash"
    return [base / "files", base / "info"]


def scan_targets() -> list[CleanTarget]:
    """Full scan: structure + sizes (uses du when available)."""
    targets = scan_targets_structure_only()
    for t in targets:
        measure_target(t)
    return targets


def measure_target(t: CleanTarget) -> CleanTarget:
    """Fill t.size for one target (safe to call from a worker thread)."""
    if t.kind == "apt":
        t.size = _apt_cache_size()
        return t
    if t.kind == "pkgcache":
        t.size = _pacman_cache_size()
        return t
    if t.kind == "journal":
        t.size = _journal_size()
        return t
    if t.kind == "residual":
        t.size = _count_residual_packages()
        t.title = f"Residual package configs ({t.size})"
        return t
    if t.kind == "orphans":
        t.size = _count_orphan_packages()
        t.title = f"Orphaned packages ({t.size})"
        return t
    total = 0
    for p in t.paths:
        total += dir_size(p)
    t.size = total
    return t


def list_target_stubs() -> list[CleanTarget]:
    """Return targets with size=0 quickly (UI skeleton before measuring)."""
    targets = scan_targets_structure_only()
    return targets


def scan_targets_structure_only() -> list[CleanTarget]:
    """Build the Linux cleaner catalogue without walking file contents.

    Rules are derived from Kudu's MIT-licensed Linux catalogue.  Only paths
    that exist are exposed.  Potentially disruptive targets stay unselected.
    """
    targets: list[CleanTarget] = []
    seen_paths: set[str] = set()

    def add(target: CleanTarget, *, include_missing: bool = False) -> None:
        paths: list[Path] = []
        for path in target.paths:
            key = str(path)
            if key in seen_paths:
                continue
            if path.is_symlink():
                continue
            if include_missing or path.exists():
                paths.append(path)
                seen_paths.add(key)
        target.paths = paths
        if paths or target.kind in {"journal", "residual"}:
            targets.append(target)

    # Browser caches are kept separate from app caches so users can review
    # them explicitly. Personal data (cookies, history and sessions) is never
    # part of these targets.
    for target in _browser_targets():
        add(target)

    for filename, category in (
        ("apps.json", "Applications"),
        ("gaming.json", "Gaming"),
        ("gpu-cache.json", "GPU caches"),
    ):
        data = _load_rule(filename)
        for item in data.get("apps", []):
            paths = _expand_rule_paths(item.get("paths", []), item.get("childSubdir"))
            add(
                CleanTarget(
                    id=f"rule-{_slug(category)}-{_slug(item.get('id') or item.get('name') or 'cache')}",
                    title=str(item.get("name") or "Application cache"),
                    description=str(item.get("description") or "Rebuildable cached data"),
                    paths=paths,
                    selected=False,
                    category=category,
                    risk="moderate",
                )
            )

    for target in _steam_targets():
        add(target)

    system_rules = _load_rule("system.json")
    for item in system_rules.get("cleanTargets", []):
        raw = str(item.get("path") or "")
        # Active temporary directories can contain live sockets and in-use
        # files. SysCare intentionally does not offer blind deletion here.
        if raw in {"${TMPDIR}", "/tmp", "/var/tmp"}:
            continue
        title = str(item.get("subcategory") or "System cache")
        needs_root = bool(item.get("needsAdmin"))
        path = _expand_path(raw)
        kind = "path"
        target_id = f"system-{_slug(title)}"
        if title == "APT Package Cache":
            kind, target_id = "apt", "apt-cache"
        elif title == "Pacman Package Cache":
            kind, target_id = "pkgcache", "pacman-cache"
        elif title == "Journal Logs":
            kind, target_id = "journal", "journal"
            title = "System journal (vacuum to 50M)"
        paths = _expand_child_dirs(path, item.get("childSubdir"))
        add(
            CleanTarget(
                id=target_id,
                title=title,
                description=str(item.get("description") or path),
                paths=paths,
                needs_root=needs_root,
                kind=kind,
                selected=(title == "Thumbnail Cache"),
                category="System",
                risk="advanced" if needs_root else "safe",
            ),
            include_missing=kind == "journal",
        )

    for item in system_rules.get("singleFileTargets", []):
        path = _expand_path(str(item.get("path") or ""))
        add(
            CleanTarget(
                id=f"system-{_slug(item.get('subcategory') or path.name)}",
                title=str(item.get("subcategory") or path.name),
                description=str(item.get("description") or path),
                paths=[path],
                selected=False,
                category="System",
                risk="moderate",
            )
        )

    trash_paths = [p for p in _trash_dirs() if p.exists()]
    targets.append(
        CleanTarget(
            id="trash",
            title="Trash",
            description="User trash (files + metadata)",
            paths=trash_paths,
            kind="trash",
            selected=False,
            category="System",
            risk="moderate",
        )
    )
    if which("dpkg-query"):
        targets.append(
            CleanTarget(
                id="residual",
                title="Residual package configs",
                description="dpkg residual configs (rc)",
                needs_root=True,
                kind="residual",
                selected=False,
                category="Packages",
                risk="advanced",
            )
        )
    if which("pacman"):
        targets.append(
            CleanTarget(
                id="orphans",
                title="Orphaned packages",
                description="Installed as dependencies, no longer required (pacman -Qtdq)",
                needs_root=True,
                kind="orphans",
                selected=False,
                category="Packages",
                risk="advanced",
            )
        )
    targets.sort(key=lambda t: (t.category, t.title.lower()))
    return targets


def _rules_dir() -> Path:
    return Path(__file__).with_name("cleaner_rules")


def _load_rule(filename: str) -> dict:
    try:
        return json.loads((_rules_dir() / filename).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        log.exception("could not load cleaner rule %s", filename)
        return {}


def _expand_path(value: str) -> Path:
    replacements = {
        "${HOME}": str(Path.home()),
        "${CONFIG}": str(xdg_config_home()),
        "${CACHE}": str(xdg_cache_home()),
        "${LOCAL_SHARE}": str(xdg_data_home()),
        "${TMPDIR}": os.environ.get("TMPDIR", "/tmp"),
    }
    for token, replacement in replacements.items():
        value = value.replace(token, replacement)
    return Path(os.path.expandvars(os.path.expanduser(value)))


def _expand_child_dirs(base: Path, child_subdir: str | None) -> list[Path]:
    if base.is_symlink():
        return []
    if not child_subdir:
        return [base]
    if not base.is_dir():
        return []
    result: list[Path] = []
    try:
        for child in base.iterdir():
            if child.is_symlink():
                continue
            target = child / child_subdir
            if target.exists() and not target.is_symlink():
                result.append(target)
    except OSError:
        pass
    return result


def _expand_rule_paths(values: list[str], child_subdir: str | None = None) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        paths.extend(_expand_child_dirs(_expand_path(str(value)), child_subdir))
    return paths


def _slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-") or "target"


def _browser_targets() -> list[CleanTarget]:
    data = _load_rule("browsers.json")
    cache_defs = data.get("chromiumCacheDirs", {})
    profile_dirs = cache_defs.get("profile", [])
    shared_dirs = cache_defs.get("shared", [])
    targets: list[CleanTarget] = []
    for browser in data.get("chromium", []):
        base = _expand_path(str(browser.get("base") or ""))
        paths: list[Path] = []
        if base.is_dir() and not base.is_symlink():
            profiles = [base / "Default"] if not (base / "Default").is_symlink() else []
            try:
                profiles.extend(p for p in base.glob("Profile *") if p.is_dir() and not p.is_symlink())
            except OSError:
                pass
            for profile in profiles:
                paths.extend(profile / str(entry.get("dir")) for entry in profile_dirs)
            paths.extend(base / str(entry.get("dir")) for entry in shared_dirs)
        present = [p for p in paths if p.exists()]
        if present:
            name = str(browser.get("key") or "Chromium").replace("GX", " GX").title()
            targets.append(
                CleanTarget(
                    id=f"browser-{_slug(browser.get('key'))}",
                    title=f"{name} cache",
                    description="Cache, code cache and GPU cache; no site storage, history or cookies",
                    paths=present,
                    selected=False,
                    category="Browsers",
                    risk="moderate",
                )
            )

    firefox_defs = []
    firefox = data.get("firefox")
    if isinstance(firefox, dict):
        firefox_defs.append(("Firefox", firefox.get("cache")))
    for fork in data.get("firefoxForks", []):
        firefox_defs.append((str(fork.get("key") or "Firefox fork").title(), fork.get("cache")))
    for name, cache_value in firefox_defs:
        if not cache_value:
            continue
        base = _expand_path(str(cache_value))
        paths: list[Path] = []
        if base.is_dir() and not base.is_symlink():
            try:
                paths = [p / "cache2" for p in base.iterdir() if not p.is_symlink() and (p / "cache2").is_dir() and not (p / "cache2").is_symlink()]
            except OSError:
                pass
        if paths:
            targets.append(
                CleanTarget(
                    id=f"browser-{_slug(name)}",
                    title=f"{name} cache",
                    description="Web content cache only; no history, cookies or saved sessions",
                    paths=paths,
                    selected=False,
                    category="Browsers",
                    risk="moderate",
                )
            )
    return targets


def _steam_targets() -> list[CleanTarget]:
    data = _load_rule("steam.json")
    patterns = {str(value).lower() for value in data.get("redistPatterns", [])}
    paths: list[Path] = []
    for value in data.get("libraries", []):
        common = _expand_path(str(value)) / "common"
        if not common.is_dir() or common.is_symlink():
            continue
        try:
            games = [path for path in common.iterdir() if path.is_dir() and not path.is_symlink()]
        except OSError:
            continue
        for game in games:
            try:
                for child in game.iterdir():
                    name = child.name.lower()
                    if child.is_dir() and not child.is_symlink() and any(pattern in name for pattern in patterns):
                        paths.append(child)
            except OSError:
                continue
    if not paths:
        return []
    return [
        CleanTarget(
            id="gaming-steam-redistributables",
            title="Steam redistributable installers",
            description="Bundled DirectX/.NET/VC runtimes; Steam can download them again",
            paths=paths,
            selected=False,
            category="Gaming",
            risk="advanced",
        )
    ]


def _apt_cache_size() -> int:
    arch = Path("/var/cache/apt/archives")
    if not arch.is_dir():
        return 0
    total = 0
    try:
        for p in arch.glob("*.deb"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        partial = arch / "partial"
        if partial.is_dir():
            total += dir_size(partial)
    except OSError:
        pass
    return total


def _pacman_cache_size() -> int:
    cache = Path("/var/cache/pacman/pkg")
    if not cache.is_dir():
        return 0
    return dir_size(cache)


def _journal_size() -> int:
    # journalctl --disk-usage
    jctl = which("journalctl")
    if not jctl:
        return 0
    try:
        r = run([jctl, "--disk-usage"], timeout=15)
        # "Archived and active journals take up 120.5M in the file system."
        text = (r.stdout or "") + (r.stderr or "")
        for token in text.replace(",", ".").split():
            if token[-1:] in "KMGTP" and any(c.isdigit() for c in token):
                num = "".join(c for c in token if c.isdigit() or c == ".")
                unit = token[-1]
                try:
                    val = float(num)
                except ValueError:
                    continue
                mult = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}.get(
                    unit, 1
                )
                return int(val * mult)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _count_residual_packages() -> int:
    try:
        r = run(["dpkg-query", "-W", "-f=${db:Status-Abbrev} ${Package}\\n"], timeout=30)
        n = 0
        for line in (r.stdout or "").splitlines():
            if line.startswith("rc "):
                n += 1
        return n
    except Exception:  # noqa: BLE001
        return 0


def _list_residual_packages() -> list[str]:
    try:
        r = run(["dpkg-query", "-W", "-f=${db:Status-Abbrev} ${Package}\\n"], timeout=30)
        pkgs = []
        for line in (r.stdout or "").splitlines():
            if line.startswith("rc "):
                pkgs.append(line.split(None, 1)[1].strip())
        return pkgs
    except Exception:  # noqa: BLE001
        return []


def _count_orphan_packages() -> int:
    return len(_list_orphan_packages())


def _list_orphan_packages() -> list[str]:
    pacman = which("pacman")
    if not pacman:
        return []
    try:
        r = run([pacman, "-Qtdq"], timeout=30)
        return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        return []


@dataclass
class CleanResult:
    target_id: str
    ok: bool
    message: str
    freed: int = 0


def clean_targets(targets: list[CleanTarget]) -> list[CleanResult]:
    """Clean selected targets. All root ops share a single pkexec (one password)."""
    selected = [t for t in targets if t.selected]
    if not selected:
        return []

    results: list[CleanResult] = []
    user_targets = [t for t in selected if not t.needs_root]
    root_targets = [t for t in selected if t.needs_root]

    for t in user_targets:
        try:
            results.append(_clean_user(t))
        except Exception as e:  # noqa: BLE001
            log.exception("clean failed: %s", t.id)
            results.append(CleanResult(t.id, False, str(e), 0))

    if root_targets:
        try:
            results.extend(_clean_root_batch(root_targets))
        except Exception as e:  # noqa: BLE001
            log.exception("root clean batch failed")
            for t in root_targets:
                results.append(CleanResult(t.id, False, str(e), 0))

    # Keep original selection order
    order = {t.id: i for i, t in enumerate(selected)}
    results.sort(key=lambda r: order.get(r.target_id, 999))
    return results


def _clean_user(t: CleanTarget) -> CleanResult:
    """User-owned paths only (no pkexec)."""
    freed = 0
    errors: list[str] = []
    for p in t.paths:
        if p.is_symlink():
            errors.append(f"refusing to follow symlink: {p}")
            continue
        if t.kind == "trash" or p.is_dir():
            f, errs = empty_dir_contents(p)
            freed += f
            errors.extend(errs)
        else:
            try:
                sz = p.stat().st_size if p.exists() else 0
            except OSError:
                sz = 0
            ok, msg = safe_rm_tree(p)
            if ok:
                freed += sz
            else:
                errors.append(msg)
    if errors:
        return CleanResult(t.id, False, "; ".join(errors[:3]), freed)
    return CleanResult(t.id, True, f"Freed ~{human_bytes(freed)}", freed)


def _clean_root_batch(targets: list[CleanTarget]) -> list[CleanResult]:
    """One privileged shell for all root clean steps → one password prompt."""
    if os.geteuid() == 0:
        # Already root — run steps directly without batching markers
        out: list[CleanResult] = []
        for t in targets:
            out.append(_clean_root_step_local(t))
        return out

    before_sizes = {t.id: t.size for t in targets}
    residual_pkgs: list[str] = []
    orphan_pkgs: list[str] = []
    for t in targets:
        if t.kind == "residual":
            residual_pkgs = _list_residual_packages()
        if t.kind == "orphans":
            orphan_pkgs = _list_orphan_packages()

    script_lines = [
        "set +e",
        "export LC_ALL=C",
    ]
    for t in targets:
        tid = t.id
        script_lines.append(f"echo 'SYSCARE_BEGIN:{tid}'")
        if t.kind == "apt":
            apt = which("apt-get") or which("apt") or "apt-get"
            script_lines.append(f"{sh_quote(apt)} clean")
            script_lines.append(f"rc=$?; echo SYSCARE_RC:{tid}:$rc")
        elif t.kind == "pkgcache":
            pacman = which("pacman") or "pacman"
            script_lines.append(f"{sh_quote(pacman)} -Sc --noconfirm")
            script_lines.append(f"rc=$?; echo SYSCARE_RC:{tid}:$rc")
        elif t.kind == "journal":
            jctl = which("journalctl") or "journalctl"
            script_lines.append(f"{sh_quote(jctl)} --vacuum-size=50M")
            script_lines.append(f"rc=$?; echo SYSCARE_RC:{tid}:$rc")
        elif t.kind == "residual":
            if not residual_pkgs:
                script_lines.append(f"echo SYSCARE_RC:{tid}:0")
                script_lines.append(f"echo SYSCARE_MSG:{tid}:no-residual")
            else:
                pkgs_q = " ".join(sh_quote(p) for p in residual_pkgs)
                script_lines.append(f"dpkg --purge {pkgs_q}")
                script_lines.append(f"rc=$?; echo SYSCARE_RC:{tid}:$rc")
                script_lines.append(f"echo SYSCARE_MSG:{tid}:purged-{len(residual_pkgs)}")
        elif t.kind == "orphans":
            if not orphan_pkgs:
                script_lines.append(f"echo SYSCARE_RC:{tid}:0")
                script_lines.append(f"echo SYSCARE_MSG:{tid}:no-orphans")
            else:
                pkgs_q = " ".join(sh_quote(p) for p in orphan_pkgs)
                pacman = which("pacman") or "pacman"
                script_lines.append(f"{sh_quote(pacman)} -Rns --noconfirm -- {pkgs_q}")
                script_lines.append(f"rc=$?; echo SYSCARE_RC:{tid}:$rc")
                script_lines.append(f"echo SYSCARE_MSG:{tid}:removed-{len(orphan_pkgs)}")
        else:
            # path clean (e.g. /var/crash)
            script_lines.append("rc=0")
            for p in t.paths:
                quoted = sh_quote(str(p))
                script_lines.append(f"if [ -L {quoted} ]; then rc=1")
                script_lines.append(f"elif [ -d {quoted} ]; then find {quoted} -mindepth 1 -maxdepth 1 -exec rm -rf -- {{}} + || rc=$?")
                script_lines.append(f"elif [ -f {quoted} ]; then rm -f -- {quoted} || rc=$?")
                script_lines.append("fi")
            script_lines.append(f"echo SYSCARE_RC:{tid}:$rc")
        script_lines.append(f"echo 'SYSCARE_END:{tid}'")

    script = "\n".join(script_lines) + "\n"
    r = run_privileged(["bash", "-c", script], timeout=600)
    text = (r.stdout or "") + "\n" + (r.stderr or "")

    # If user cancelled pkexec, returncode often 126/127 or empty with non-zero
    if r.returncode != 0 and "SYSCARE_BEGIN:" not in text:
        msg = (r.stderr or r.stdout or "privilege elevation failed / cancelled").strip()[:300]
        return [CleanResult(t.id, False, msg, 0) for t in targets]

    rc_map: dict[str, int] = {}
    msg_map: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("SYSCARE_RC:"):
            # SYSCARE_RC:id:code
            parts = line.split(":", 2)
            if len(parts) == 3:
                try:
                    rc_map[parts[1]] = int(parts[2])
                except ValueError:
                    rc_map[parts[1]] = 1
        elif line.startswith("SYSCARE_MSG:"):
            parts = line.split(":", 2)
            if len(parts) == 3:
                msg_map[parts[1]] = parts[2]

    results: list[CleanResult] = []
    for t in targets:
        code = rc_map.get(t.id)
        if code is None:
            # step may not have run if script aborted early
            results.append(CleanResult(t.id, False, "step did not complete", 0))
            continue
        if code != 0:
            results.append(CleanResult(t.id, False, f"exit {code}", 0))
            continue
        # estimate freed
        if t.kind == "apt":
            after = _apt_cache_size()
            freed = max(0, before_sizes.get(t.id, 0) - after)
            results.append(CleanResult(t.id, True, "APT cache cleaned", freed))
        elif t.kind == "pkgcache":
            after = _pacman_cache_size()
            freed = max(0, before_sizes.get(t.id, 0) - after)
            results.append(CleanResult(t.id, True, "Pacman cache cleaned", freed))
        elif t.kind == "journal":
            after = _journal_size()
            freed = max(0, before_sizes.get(t.id, 0) - after)
            results.append(CleanResult(t.id, True, "Journal vacuumed to 50M", freed))
        elif t.kind == "residual":
            if msg_map.get(t.id) == "no-residual":
                results.append(CleanResult(t.id, True, "No residual packages", 0))
            else:
                n = len(residual_pkgs)
                results.append(CleanResult(t.id, True, f"Purged {n} residual package(s)", 0))
        elif t.kind == "orphans":
            if msg_map.get(t.id) == "no-orphans":
                results.append(CleanResult(t.id, True, "No orphaned packages", 0))
            else:
                n = len(orphan_pkgs)
                results.append(CleanResult(t.id, True, f"Removed {n} orphaned package(s)", 0))
        else:
            freed = before_sizes.get(t.id, 0)
            results.append(CleanResult(t.id, True, f"Freed ~{human_bytes(freed)}", freed))
    return results


def _clean_root_step_local(t: CleanTarget) -> CleanResult:
    """Run one root step when already euid 0."""
    if t.kind == "apt":
        apt = which("apt-get") or which("apt")
        if not apt:
            return CleanResult(t.id, False, "apt not found", 0)
        before = t.size
        r = run([apt, "clean"], timeout=180)
        if r.returncode != 0:
            return CleanResult(t.id, False, (r.stderr or r.stdout or "apt clean failed")[:400], 0)
        return CleanResult(t.id, True, "APT cache cleaned", max(0, before - _apt_cache_size()))
    if t.kind == "pkgcache":
        pacman = which("pacman")
        if not pacman:
            return CleanResult(t.id, False, "pacman not found", 0)
        before = t.size
        r = run([pacman, "-Sc", "--noconfirm"], timeout=180)
        if r.returncode != 0:
            return CleanResult(t.id, False, (r.stderr or r.stdout or "pacman -Sc failed")[:400], 0)
        return CleanResult(t.id, True, "Pacman cache cleaned", max(0, before - _pacman_cache_size()))
    if t.kind == "journal":
        jctl = which("journalctl")
        if not jctl:
            return CleanResult(t.id, False, "journalctl not found", 0)
        before = t.size
        r = run([jctl, "--vacuum-size=50M"], timeout=120)
        if r.returncode != 0:
            return CleanResult(t.id, False, (r.stderr or r.stdout or "vacuum failed")[:400], 0)
        return CleanResult(t.id, True, "Journal vacuumed to 50M", max(0, before - _journal_size()))
    if t.kind == "residual":
        pkgs = _list_residual_packages()
        if not pkgs:
            return CleanResult(t.id, True, "No residual packages", 0)
        r = run(["dpkg", "--purge", *pkgs], timeout=300)
        if r.returncode != 0:
            return CleanResult(t.id, False, (r.stderr or r.stdout or "purge failed")[:400], 0)
        return CleanResult(t.id, True, f"Purged {len(pkgs)} residual package(s)", 0)
    if t.kind == "orphans":
        pkgs = _list_orphan_packages()
        if not pkgs:
            return CleanResult(t.id, True, "No orphaned packages", 0)
        pacman = which("pacman") or "pacman"
        r = run([pacman, "-Rns", "--noconfirm", "--", *pkgs], timeout=300)
        if r.returncode != 0:
            return CleanResult(t.id, False, (r.stderr or r.stdout or "orphan remove failed")[:400], 0)
        return CleanResult(t.id, True, f"Removed {len(pkgs)} orphaned package(s)", 0)
    freed = 0
    errors: list[str] = []
    for p in t.paths:
        f, errs = empty_dir_contents(p)
        freed += f
        errors.extend(errs)
    if errors:
        return CleanResult(t.id, False, "; ".join(errors[:3]), freed)
    return CleanResult(t.id, True, f"Freed ~{human_bytes(freed)}", freed)


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"
