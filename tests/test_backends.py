from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from syscare import cleaner, history, security, storage_tools, updater
from syscare.util import empty_dir_contents


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


class SecurityTests(unittest.TestCase):
    def test_unknown_hardening_ids_are_ignored(self) -> None:
        self.assertEqual(security.apply_hardening(["not-a-setting"]), (0, []))


if __name__ == "__main__":
    unittest.main()
