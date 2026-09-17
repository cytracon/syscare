from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from syscare import cleaner, history, security, storage_tools, updater
from syscare.util import empty_dir_contents, run_privileged


class CleanerCatalogueTests(unittest.TestCase):
    def test_catalogue_has_unique_ids_and_never_blindly_cleans_temp(self) -> None:
        targets = cleaner.scan_targets_structure_only()
        self.assertEqual(len({target.id for target in targets}), len(targets))
        exposed = {str(path) for target in targets for path in target.paths}
        self.assertNotIn("/tmp", exposed)
        self.assertNotIn("/var/tmp", exposed)

    def test_expand_path_uses_xdg_locations(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "/tmp/syscare-cache"}):
            self.assertEqual(cleaner._expand_path("${CACHE}/tool"), Path("/tmp/syscare-cache/tool"))

    def test_xdg_browser_and_dev_caches_are_catalogued(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config"
            cache = root / "cache"
            chrome_http = cache / "google-chrome" / "Default" / "Cache"
            chrome_sw = (
                config / "google-chrome" / "Default" / "Service Worker" / "CacheStorage"
            )
            yay = cache / "yay"
            playwright = cache / "ms-playwright"
            mise = cache / "mise"
            for path in (chrome_http, chrome_sw, yay, playwright, mise):
                path.mkdir(parents=True)
            env = {
                "HOME": str(root),
                "XDG_CONFIG_HOME": str(config),
                "XDG_CACHE_HOME": str(cache),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                targets = cleaner.scan_targets_structure_only()
            exposed = {str(path) for target in targets for path in target.paths}
            self.assertIn(str(chrome_http), exposed)
            self.assertIn(str(chrome_sw), exposed)
            self.assertIn(str(yay), exposed)
            self.assertIn(str(playwright), exposed)
            self.assertIn(str(mise), exposed)
            chrome = next(target for target in targets if target.id == "browser-chrome")
            self.assertEqual(chrome.risk, "moderate")
            self.assertFalse(chrome.selected)
            self.assertNotIn(str(cache / "google-chrome" / "Default" / "Storage"), exposed)


class HistoryTests(unittest.TestCase):
    def test_history_round_trip_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"SYSCARE_STATE_HOME": temp}
        ):
            history.add_entry("test", "ok", "worked", reclaimed=42)
            entries = history.list_entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].reclaimed, 42)
            self.assertEqual(entries[0].summary, "worked")
            history.clear()
            self.assertEqual(history.list_entries(), [])


class StorageTests(unittest.TestCase):
    def test_duplicate_finder_hashes_full_contents(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temp:
            root = Path(temp)
            payload = b"same" * 300_000
            (root / "a.bin").write_bytes(payload)
            (root / "b.bin").write_bytes(payload)
            (root / "different.bin").write_bytes(b"else" * 300_000)
            groups = storage_tools.find_duplicates(root, min_size=1024)
            self.assertEqual(len(groups), 1)
            self.assertEqual({path.name for path in groups[0].paths}, {"a.bin", "b.bin"})

    def test_delete_rejects_paths_outside_home(self) -> None:
        removed, errors = storage_tools.delete_user_paths([Path("/etc/passwd")])
        self.assertEqual(removed, 0)
        self.assertTrue(errors)

    def test_cleaner_refuses_to_follow_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temp:
            root = Path(temp)
            target = root / "target"
            target.mkdir()
            marker = target / "keep"
            marker.write_text("data")
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            freed, errors = empty_dir_contents(link)
            self.assertEqual(freed, 0)
            self.assertTrue(errors)
            self.assertTrue(marker.exists())

    def test_cleaner_deletes_unreadable_subdir(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temp:
            root = Path(temp)
            sub = root / "sub"
            sub.mkdir()
            secret = sub / "file"
            secret.write_text("data")
            os.chmod(sub, 0o000)
            try:
                _freed, _errors = empty_dir_contents(root)
            finally:
                if sub.exists():
                    os.chmod(sub, 0o700)
            self.assertFalse(secret.exists())
            self.assertFalse(sub.exists())
            self.assertTrue(root.exists())

    def test_clean_targets_honors_passed_list_not_selected_flag(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temp:
            cache = Path(temp) / "cache"
            cache.mkdir()
            payload = cache / "blob"
            payload.write_text("hello")
            target = cleaner.CleanTarget(
                id="test-cache",
                title="Test cache",
                description="",
                paths=[cache],
                selected=False,
                kind="path",
            )
            results = cleaner.clean_targets([target])
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].ok)
            self.assertFalse(payload.exists())
            self.assertTrue(cache.exists())

    def test_root_batch_wipes_pacman_cache_dir_not_sc(self) -> None:
        target = cleaner.CleanTarget(
            id="pacman-cache",
            title="Pacman Package Cache",
            description="",
            paths=[Path("/var/cache/pacman/pkg")],
            needs_root=True,
            selected=True,
            kind="pkgcache",
        )
        captured: dict[str, str] = {}

        def fake_privileged(cmd, timeout=None):  # noqa: ARG001
            captured["script"] = cmd[2]
            return mock.Mock(returncode=0, stdout="SYSCARE_BEGIN:pacman-cache\nSYSCARE_RC:pacman-cache:0\nSYSCARE_END:pacman-cache\n", stderr="")

        with mock.patch("syscare.cleaner.run_privileged", side_effect=fake_privileged), mock.patch(
            "syscare.cleaner._pacman_cache_size", return_value=0
        ), mock.patch("syscare.cleaner.os.geteuid", return_value=1):
            results = cleaner._clean_root_batch([target])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)
        script = captured.get("script", "")
        self.assertNotIn(" -Sc ", script)
        self.assertIn("find '/var/cache/pacman/pkg'", script)
        self.assertIn("rm -rf", script)


class UpdaterTests(unittest.TestCase):
    @mock.patch("syscare.updater.which")
    @mock.patch("syscare.updater.run")
    def test_parses_pacman_updates(self, run_mock, which_mock) -> None:
        def which_side(name: str) -> str | None:
            if name == "checkupdates":
                return "/usr/bin/checkupdates"
            if name == "omarchy":
                return None
            return None

        which_mock.side_effect = which_side
        run_mock.return_value = mock.Mock(
            returncode=0,
            stdout="linux 6.16.1-1 -> 6.16.2-1\n",
            stderr="",
        )
        items, errors = updater.list_updates()
        self.assertEqual(errors, [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].manager, "Pacman")
        self.assertEqual(items[0].package, "linux")
        self.assertEqual(items[0].available, "6.16.2-1")


class PrivilegedExecTests(unittest.TestCase):
    @mock.patch("syscare.util.run")
    @mock.patch("syscare.util.which")
    @mock.patch("syscare.util.os.geteuid", return_value=1)
    def test_pkexec_uses_absolute_program_path(self, _euid, which_mock, run_mock) -> None:
        def which_side(name: str) -> str | None:
            return {"pkexec": "/usr/bin/pkexec", "bash": "/usr/bin/bash"}.get(name)

        which_mock.side_effect = which_side
        run_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        run_privileged(["bash", "-c", "echo hi"])
        run_mock.assert_called_once()
        cmd = run_mock.call_args[0][0]
        self.assertEqual(cmd[:3], ["/usr/bin/pkexec", "/usr/bin/bash", "-c"])


class SecurityTests(unittest.TestCase):
    def test_unknown_hardening_ids_are_ignored(self) -> None:
        self.assertEqual(security.apply_hardening(["not-a-setting"]), (0, []))


if __name__ == "__main__":
    unittest.main()
