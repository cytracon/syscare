"""GTK pages for the Linux-native maintenance tools added in SysCare 0.2."""

from __future__ import annotations

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import history, network_tools, scheduler, security, storage_tools, updater
from .util import human_bytes


def _toast(host: Adw.ToastOverlay | None, message: str) -> None:
    if host is not None:
        host.add_toast(Adw.Toast(title=message, timeout=4))


def _confirm(parent: Gtk.Widget, heading: str, body: str, label: str, callback) -> None:
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("ok", label)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _dialog, response: callback() if response == "ok" else None)
    root = parent.get_root()
    dialog.present(root if root is not None else parent)


class _AsyncPage:
    _alive = True

    def _start(self, work, done) -> None:
        def runner() -> None:
            try:
                result = work()
                error = None
            except Exception as exc:  # noqa: BLE001
                result, error = None, str(exc)

            def finish() -> bool:
                if self._alive:
                    done(result, error)
                return False

            GLib.idle_add(finish)

        threading.Thread(target=runner, daemon=True).start()

    def dispose_timers(self) -> None:
        self._alive = False


class SecurityPage(_AsyncPage, Gtk.Box):
    def __init__(self, toast: Adw.ToastOverlay | None = None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._alive = True
        self._toast = toast
        self._findings: list[security.MalwareFinding] = []
        self._hardening_warnings: list[str] = []

        bar = Gtk.Box(spacing=8, margin_top=12, margin_bottom=8, margin_start=16, margin_end=16)
        self.append(bar)
        audit = Gtk.Button(label="Run security audit")
        audit.add_css_class("suggested-action")
        audit.connect("clicked", lambda *_: self.run_audit())
        bar.append(audit)
        scan = Gtk.Button(label="Scan Downloads for malware")
        scan.connect("clicked", lambda *_: self.scan_malware())
        bar.append(scan)
        self._harden = Gtk.Button(label="Apply recommended hardening", sensitive=False)
        self._harden.connect("clicked", lambda *_: self.apply_hardening())
        bar.append(self._harden)
        self._quarantine = Gtk.Button(label="Quarantine findings", sensitive=False)
        self._quarantine.add_css_class("destructive-action")
        self._quarantine.connect("clicked", lambda *_: self.quarantine())
        bar.append(self._quarantine)
        self._status = Gtk.Label(label="Ready", xalign=0, hexpand=True)
        self._status.add_css_class("dim-label")
        bar.append(self._status)

        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(scroll)
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._list.set_margin_start(16)
        self._list.set_margin_end(16)
        self._list.set_margin_bottom(16)
        scroll.set_child(self._list)
        GLib.idle_add(self.run_audit)

    def _clear(self) -> None:
        while row := self._list.get_row_at_index(0):
            self._list.remove(row)

    def run_audit(self) -> None:
        self._status.set_label("Auditing Linux security posture…")

        def done(result, error) -> None:
            if error:
                self._status.set_label(error)
                return
            checks: list[security.SecurityCheck] = result
            self._hardening_warnings = [
                check.id
                for check in checks
                if check.status == "warning" and check.id in security.hardening_ids()
            ]
            self._harden.set_sensitive(bool(self._hardening_warnings))
            self._clear()
            for check in checks:
                row = Adw.ActionRow(title=check.title, subtitle=check.detail)
                badge = Gtk.Label(label={"pass": "PASS", "warning": "CHECK"}.get(check.status, "INFO"))
                badge.add_css_class("success" if check.status == "pass" else "warning" if check.status == "warning" else "dim-label")
                row.add_suffix(badge)
                self._list.append(row)
            warnings = sum(1 for check in checks if check.status == "warning")
            self._status.set_label(f"Audit complete · {warnings} item(s) need review")
            history.add_entry("Security audit", "warning" if warnings else "ok", self._status.get_label(), details={"warnings": warnings})

        self._start(security.audit, done)

    def scan_malware(self) -> None:
        self._status.set_label("Scanning Downloads with ClamAV…")

        def done(result, error) -> None:
            if error:
                self._status.set_label(error)
                return
            findings, message = result
            self._findings = findings
            self._quarantine.set_sensitive(bool(findings))
            self._clear()
            if findings:
                for finding in findings:
                    row = Adw.ActionRow(title=finding.threat, subtitle=str(finding.path))
                    self._list.append(row)
            else:
                self._list.append(Adw.ActionRow(title="Malware scan", subtitle=message))
            self._status.set_label(message)
            history.add_entry("Malware scan", "warning" if findings else "ok", message, details={"findings": len(findings)})

        self._start(security.quick_malware_scan, done)

    def apply_hardening(self) -> None:
        ids = list(self._hardening_warnings)
        if not ids:
            _toast(self._toast, "Recommended hardening is already applied")
            return

        def execute() -> None:
            self._status.set_label("Applying selected privacy and kernel hardening…")

            def done(result, error) -> None:
                if error:
                    self._status.set_label(error)
                    return
                applied, errors = result
                message = f"Applied {applied} hardening setting(s)"
                if errors:
                    message += f" · {len(errors)} error(s)"
                history.add_entry("Security hardening", "warning" if errors else "ok", message)
                _toast(self._toast, message)
                self.run_audit()

            self._start(lambda: security.apply_hardening(ids), done)

        _confirm(
            self,
            "Apply recommended Linux hardening?",
            f"This changes {len(ids)} GNOME privacy and/or kernel sysctl setting(s). "
            "Kernel changes are stored in /etc/sysctl.d/99-syscare-hardening.conf and require authentication.",
            "Apply hardening",
            execute,
        )

    def quarantine(self) -> None:
        findings = list(self._findings)
        if not findings:
            return

        def execute() -> None:
            self._status.set_label("Quarantining…")

            def done(result, error) -> None:
                if error:
                    self._status.set_label(error)
                    return
                moved, errors = result
                message = f"Quarantined {moved} file(s)"
                if errors:
                    message += f" · {len(errors)} error(s)"
                self._status.set_label(message)
                self._findings = []
                self._quarantine.set_sensitive(False)
                history.add_entry("Malware quarantine", "warning" if errors else "ok", message)
                _toast(self._toast, message)
                self.run_audit()

            self._start(lambda: security.quarantine(findings), done)

        _confirm(self, "Quarantine detected files?", "Files will be moved to SysCare's private quarantine directory.", "Quarantine", execute)


class UpdaterPage(_AsyncPage, Gtk.Box):
    def __init__(self, toast: Adw.ToastOverlay | None = None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._alive = True
        self._toast = toast
        self._items: list[updater.UpdateItem] = []
        bar = Gtk.Box(spacing=8, margin_top=12, margin_bottom=8, margin_start=16, margin_end=16)
        self.append(bar)
        check = Gtk.Button(label="Check for updates")
        check.add_css_class("suggested-action")
        check.connect("clicked", lambda *_: self.refresh())
        bar.append(check)
        metadata = Gtk.Button(label="Sync pacman databases")
        metadata.connect("clicked", lambda *_: self.refresh_metadata())
        bar.append(metadata)
        install = Gtk.Button(label="Install selected")
        install.add_css_class("destructive-action")
        install.connect("clicked", lambda *_: self.install_selected())
        bar.append(install)
        self._status = Gtk.Label(label="Ready", xalign=0, hexpand=True)
        self._status.add_css_class("dim-label")
        bar.append(self._status)

        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(scroll)
        self._model = Gtk.ListStore(bool, str, str, str, str)
        view = Gtk.TreeView(model=self._model)
        self._view = view
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._toggle)
        view.append_column(Gtk.TreeViewColumn("", toggle, active=0))
        for index, title, expand in ((1, "Manager", False), (2, "Package", True), (3, "Installed", True), (4, "Available", True)):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_expand(expand)
            view.append_column(column)
        self._empty = Gtk.Label(
            label="No pending updates.\nPacman, Omarchy, AUR and Flatpak are current.",
            justify=Gtk.Justification.CENTER,
            vexpand=True,
        )
        self._empty.add_css_class("dim-label")
        stack = Gtk.Stack()
        stack.add_named(view, "list")
        stack.add_named(self._empty, "empty")
        self._stack = stack
        scroll.set_child(stack)
        GLib.idle_add(self.refresh)

    def _toggle(self, _renderer, path: str) -> None:
        iterator = self._model.get_iter(path)
        self._model.set_value(iterator, 0, not self._model.get_value(iterator, 0))

    def refresh(self) -> None:
        self._status.set_label("Checking Pacman, Omarchy, AUR and Flatpak…")

        def done(result, error) -> None:
            if error:
                self._status.set_label(error)
                return
            items, errors = result
            self._items = items
            self._model.clear()
            for item in items:
                self._model.append([False, item.manager, item.package, item.current, item.available])
            suffix = f" · {len(errors)} warning(s)" if errors else ""
            if items:
                self._stack.set_visible_child_name("list")
                self._status.set_label(f"{len(items)} update(s) available{suffix}")
            else:
                self._stack.set_visible_child_name("empty")
                warn = f" ({len(errors)} warning(s))" if errors else ""
                self._status.set_label(f"Up to date — Pacman / Omarchy / AUR / Flatpak{warn}")

        self._start(updater.list_updates, done)

    def refresh_metadata(self) -> None:
        self._status.set_label("Syncing pacman databases (authentication may be requested)…")

        def done(result, error) -> None:
            if error:
                self._status.set_label(error)
                return
            self._status.set_label("Pacman databases synced" if result.ok else result.message[-200:])
            history.add_entry("Pacman database sync", "ok" if result.ok else "error", self._status.get_label())
            if result.ok:
                self.refresh()

        self._start(updater.refresh_metadata, done)

    def install_selected(self) -> None:
        selected: list[updater.UpdateItem] = []
        iterator = self._model.get_iter_first()
        index = 0
        while iterator is not None:
            if self._model.get_value(iterator, 0) and index < len(self._items):
                selected.append(self._items[index])
            index += 1
            iterator = self._model.iter_next(iterator)
        if not selected:
            _toast(self._toast, "Select updates first")
            return

        def execute() -> None:
            self._status.set_label(f"Installing {len(selected)} update(s)…")

            def done(result, error) -> None:
                if error:
                    self._status.set_label(error)
                    return
                message = "Updates installed" if result.ok else result.message[-250:]
                self._status.set_label(message)
                history.add_entry("Software updates", "ok" if result.ok else "error", message, details={"selected": len(selected)})
                _toast(self._toast, message)
                self.refresh()

            self._start(lambda: updater.install_updates(selected), done)

        packages = ", ".join(item.package for item in selected[:20])
        _confirm(self, "Install selected updates?", f"{packages}\n\nPacman and Omarchy updates may request administrator authentication.", "Install", execute)


class StoragePage(_AsyncPage, Gtk.Box):
    MODES = (
        ("usage", "Disk usage"),
        ("large", "Large files (≥100 MB)"),
        ("duplicates", "Duplicate files (≥1 MB)"),
        ("empty", "Empty folders"),
        ("shred", "File shredder (path above)"),
        ("databases", "Optimize SQLite databases"),
        ("disks", "Disks, S.M.A.R.T. and TRIM"),
    )

    def __init__(self, toast: Adw.ToastOverlay | None = None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._alive = True
        self._toast = toast
        self._records: list[object] = []
        bar = Gtk.Box(spacing=8, margin_top=12, margin_bottom=8, margin_start=16, margin_end=16)
        self.append(bar)
        self._path = Gtk.Entry(text=str(Path.home()), hexpand=True)
        self._path.set_placeholder_text("Directory to analyze")
        bar.append(self._path)
        self._mode = Gtk.ComboBoxText()
        for mode_id, label in self.MODES:
            self._mode.append(mode_id, label)
        self._mode.set_active_id("usage")
        self._mode.connect("changed", lambda *_: self._mode_changed())
        bar.append(self._mode)
        scan = Gtk.Button(label="Scan")
        scan.add_css_class("suggested-action")
        scan.connect("clicked", lambda *_: self.scan())
        bar.append(scan)
        self._action = Gtk.Button(label="Delete selected", sensitive=False)
        self._action.add_css_class("destructive-action")
        self._action.connect("clicked", lambda *_: self.run_action())
        bar.append(self._action)

        self._status = Gtk.Label(label="Ready", xalign=0, margin_start=16, margin_end=16, margin_bottom=8)
        self._status.add_css_class("dim-label")
        self.append(self._status)
        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(scroll)
        self._model = Gtk.ListStore(bool, str, str, str)
        view = Gtk.TreeView(model=self._model)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._toggle)
        view.append_column(Gtk.TreeViewColumn("", toggle, active=0))
        for index, title, expand in ((1, "Item", True), (2, "Path / mount", True), (3, "Size / status", False)):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_expand(expand)
            view.append_column(column)
        scroll.set_child(view)
        self._mode_changed()

    def _toggle(self, _renderer, path: str) -> None:
        iterator = self._model.get_iter(path)
        self._model.set_value(iterator, 0, not self._model.get_value(iterator, 0))

    def _mode_changed(self) -> None:
        mode = self._mode.get_active_id() or "usage"
        labels = {"databases": "Optimize selected", "disks": "TRIM selected", "shred": "Shred selected", "usage": "No destructive action"}
        self._action.set_label(labels.get(mode, "Delete selected"))
        self._action.set_sensitive(mode != "usage")

    def scan(self) -> None:
        mode = self._mode.get_active_id() or "usage"
        root = Path(self._path.get_text() or str(Path.home()))
        self._status.set_label("Scanning…")

        def work():
            if mode == "usage":
                return storage_tools.analyze_directory(root)
            if mode == "large":
                return storage_tools.find_large_files(root)
            if mode == "duplicates":
                return storage_tools.find_duplicates(root)
            if mode == "empty":
                return storage_tools.find_empty_folders(root)
            if mode == "shred":
                path = root.expanduser().resolve()
                if not path.is_file() or path.is_symlink():
                    raise ValueError("Enter the path of one regular file to shred")
                return [storage_tools.FileRecord(path, path.stat().st_size)]
            if mode == "databases":
                return storage_tools.database_candidates()
            return storage_tools.disk_inventory()

        def done(result, error) -> None:
            if error:
                self._status.set_label(error)
                return
            self._records = list(result)
            self._model.clear()
            if mode in {"usage", "large", "shred"}:
                for record in self._records:
                    self._model.append([False, record.path.name, str(record.path), human_bytes(record.size)])
            elif mode == "duplicates":
                for group_index, group in enumerate(self._records, 1):
                    for path_index, path in enumerate(group.paths):
                        label = f"Group {group_index}" + (" · keep" if path_index == 0 else "")
                        self._model.append([False, label, str(path), human_bytes(group.size)])
            elif mode == "empty":
                for path in self._records:
                    self._model.append([False, "Empty folder", str(path), "0 B"])
            elif mode == "databases":
                for record in self._records:
                    self._model.append([False, record.application, str(record.path), human_bytes(record.size)])
            else:
                for record in self._records:
                    media = "HDD" if record.rotational else "SSD" if record.rotational is False else "disk"
                    detail = f"{record.filesystem} · {media} · S.M.A.R.T. {record.health} · {human_bytes(record.free)} free"
                    self._model.append([False, record.device, record.mountpoint, detail])
            self._status.set_label(f"Scan complete · {len(self._records)} result(s)")

        self._start(work, done)

    def _selected_paths(self) -> list[Path]:
        paths: list[Path] = []
        iterator = self._model.get_iter_first()
        while iterator is not None:
            if self._model.get_value(iterator, 0):
                paths.append(Path(str(self._model.get_value(iterator, 2))))
            iterator = self._model.iter_next(iterator)
        return paths

    def run_action(self) -> None:
        mode = self._mode.get_active_id() or "usage"
        paths = self._selected_paths()
        if not paths:
            _toast(self._toast, "Select items first")
            return

        def execute() -> None:
            self._status.set_label("Working…")

            def work():
                if mode == "databases":
                    wanted = set(paths)
                    return storage_tools.optimize_databases([record for record in self._records if record.path in wanted])
                if mode == "disks":
                    results = [storage_tools.trim_mount(str(path)) for path in paths]
                    return sum(1 for ok, _ in results if ok), [message for ok, message in results if not ok]
                if mode == "shred":
                    results = [storage_tools.shred_file(path) for path in paths]
                    return sum(1 for ok, _ in results if ok), [message for ok, message in results if not ok]
                return storage_tools.delete_user_paths(paths, empty_dirs_only=mode == "empty")

            def done(result, error) -> None:
                if error:
                    self._status.set_label(error)
                    return
                if mode == "databases":
                    count, reclaimed, errors = result
                    message = f"Optimized {count} database(s) · reclaimed {human_bytes(reclaimed)}"
                else:
                    count, errors = result
                    message = f"Completed {count} item(s)"
                if errors:
                    message += f" · {len(errors)} error(s)"
                self._status.set_label(message)
                history.add_entry(f"Storage: {mode}", "warning" if errors else "ok", message)
                _toast(self._toast, message)
                self.scan()

            self._start(work, done)

        action = "optimize" if mode == "databases" else "TRIM" if mode == "disks" else "securely shred" if mode == "shred" else "permanently delete"
        extra = " Close the associated applications first." if mode == "databases" else " This cannot be undone." if mode == "shred" else ""
        _confirm(self, f"{action.capitalize()} selected items?", f"{len(paths)} item(s) selected. Review the list before continuing.{extra}", action.capitalize(), execute)


class NetworkPage(_AsyncPage, Gtk.Box):
    def __init__(self, toast: Adw.ToastOverlay | None = None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._alive = True
        self._toast = toast
        bar = Gtk.Box(spacing=8, margin_top=12, margin_bottom=8, margin_start=16, margin_end=16)
        self.append(bar)
        refresh = Gtk.Button(label="Refresh connections")
        refresh.add_css_class("suggested-action")
        refresh.connect("clicked", lambda *_: self.refresh())
        bar.append(refresh)
        dns = Gtk.Button(label="Flush DNS cache")
        dns.connect("clicked", lambda *_: self._simple_action("DNS cache", network_tools.flush_dns))
        bar.append(dns)
        arp = Gtk.Button(label="Clear ARP cache")
        arp.connect("clicked", lambda *_: self._simple_action("ARP cache", network_tools.clear_arp))
        bar.append(arp)
        self._profiles = Gtk.ComboBoxText()
        bar.append(self._profiles)
        delete = Gtk.Button(label="Forget Wi-Fi")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", lambda *_: self._delete_wifi())
        bar.append(delete)
        self._status = Gtk.Label(label="Ready", xalign=0, hexpand=True)
        self._status.add_css_class("dim-label")
        bar.append(self._status)

        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(scroll)
        self._model = Gtk.ListStore(str, str, str, str)
        view = Gtk.TreeView(model=self._model)
        for index, title, expand in ((0, "Protocol", False), (1, "Local", True), (2, "Remote", True), (3, "Process", True)):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_expand(expand)
            view.append_column(column)
        scroll.set_child(view)
        GLib.idle_add(self.refresh)

    def refresh(self) -> None:
        self._status.set_label("Reading connections and Wi-Fi profiles…")

        def work():
            return network_tools.established_connections(), network_tools.wifi_profiles()

        def done(result, error) -> None:
            if error:
                self._status.set_label(error)
                return
            connections, profiles = result
            self._model.clear()
            for row in connections:
                self._model.append([row.protocol, row.local, row.remote, row.process])
            self._profiles.remove_all()
            for profile in profiles:
                self._profiles.append_text(profile)
            if profiles:
                self._profiles.set_active(0)
            self._status.set_label(f"{len(connections)} established connection(s) · {len(profiles)} saved Wi-Fi profile(s)")

        self._start(work, done)

    def _simple_action(self, name: str, callback) -> None:
        self._status.set_label(f"Cleaning {name}…")

        def done(result, error) -> None:
            if error:
                self._status.set_label(error)
                return
            ok, message = result
            self._status.set_label(message)
            history.add_entry(f"Network: {name}", "ok" if ok else "error", message)
            _toast(self._toast, message)

        self._start(callback, done)

    def _delete_wifi(self) -> None:
        name = self._profiles.get_active_text()
        if not name:
            _toast(self._toast, "Select a Wi-Fi profile")
            return
        _confirm(self, "Forget saved Wi-Fi network?", f"The saved profile “{name}” will be deleted.", "Forget", lambda: self._simple_action("Wi-Fi profile", lambda: network_tools.delete_wifi_profile(name)))


class SchedulesPage(_AsyncPage, Gtk.Box):
    def __init__(self, toast: Adw.ToastOverlay | None = None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self._alive = True
        self._toast = toast
        self.set_margin_top(24)
        self.set_margin_start(24)
        self.set_margin_end(24)
        group = Adw.PreferencesGroup(title="Scheduled maintenance", description="Runs safe user-cache cleanup through a persistent systemd user timer. Root and moderate-risk targets are excluded.")
        self.append(group)
        row = Adw.ActionRow(title="Frequency")
        self._frequency = Gtk.ComboBoxText()
        for mode in ("daily", "weekly", "monthly"):
            self._frequency.append(mode, mode.capitalize())
        self._frequency.set_active_id("weekly")
        row.add_suffix(self._frequency)
        group.add(row)
        actions = Gtk.Box(spacing=8)
        enable = Gtk.Button(label="Enable schedule")
        enable.add_css_class("suggested-action")
        enable.connect("clicked", lambda *_: self.enable())
        actions.append(enable)
        disable = Gtk.Button(label="Disable")
        disable.connect("clicked", lambda *_: self.disable())
        actions.append(disable)
        self.append(actions)
        self._status = Gtk.Label(label="", xalign=0, wrap=True)
        self.append(self._status)
        GLib.idle_add(self.refresh)

    def refresh(self) -> None:
        status = scheduler.get_status()
        if status.frequency in {"daily", "weekly", "monthly"}:
            self._frequency.set_active_id(status.frequency)
        self._status.set_label(f"Status: {'enabled' if status.enabled else 'disabled'}" + (f" · next: {status.next_run}" if status.next_run else ""))

    def enable(self) -> None:
        frequency = self._frequency.get_active_id() or "weekly"
        self._run(lambda: scheduler.set_schedule(frequency))

    def disable(self) -> None:
        self._run(scheduler.disable_schedule)

    def _run(self, callback) -> None:
        def done(result, error) -> None:
            if error:
                self._status.set_label(error)
                return
            ok, message = result
            self._status.set_label(message)
            _toast(self._toast, message)
            self.refresh()

        self._start(callback, done)


class HistoryPage(Gtk.Box):
    def __init__(self, toast: Adw.ToastOverlay | None = None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._toast = toast
        bar = Gtk.Box(spacing=8, margin_top=12, margin_bottom=8, margin_start=16, margin_end=16)
        self.append(bar)
        refresh = Gtk.Button(label="Refresh")
        refresh.add_css_class("suggested-action")
        refresh.connect("clicked", lambda *_: self.refresh())
        bar.append(refresh)
        clear = Gtk.Button(label="Clear history")
        clear.add_css_class("destructive-action")
        clear.connect("clicked", lambda *_: _confirm(self, "Clear maintenance history?", "This removes SysCare's local audit trail only.", "Clear", self.clear))
        bar.append(clear)
        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(scroll)
        self._model = Gtk.ListStore(str, str, str, str, str)
        view = Gtk.TreeView(model=self._model)
        for index, title, expand in ((0, "Time", False), (1, "Action", False), (2, "Status", False), (3, "Summary", True), (4, "Reclaimed", False)):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_expand(expand)
            view.append_column(column)
        scroll.set_child(view)
        GLib.idle_add(self.refresh)

    def refresh(self) -> None:
        self._model.clear()
        for entry in history.list_entries():
            self._model.append([entry.timestamp, entry.action, entry.status, entry.summary, human_bytes(entry.reclaimed) if entry.reclaimed else "—"])

    def clear(self) -> None:
        history.clear()
        self.refresh()
        _toast(self._toast, "History cleared")

    def dispose_timers(self) -> None:
        pass
