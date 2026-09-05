"""SysCare main window — Stacer-style sidebar + stack + tray."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __app_id__, __app_name__, __version__
from .advanced_pages import (
    HistoryPage,
    NetworkPage,
    SchedulesPage,
    SecurityPage,
    StoragePage,
    UpdaterPage,
)
from .pages import (
    CleanerPage,
    DashboardPage,
    ProcessesPage,
    ResourcesPage,
    ServicesPage,
    StartupPage,
    UninstallerPage,
)
from .tray import TrayIcon

log = logging.getLogger("syscare.app")

ICON_NAME = __app_id__

CSS = """
.sidebar-list {
  background: alpha(@window_bg_color, 0.6);
}
.sidebar-list row {
  border-radius: 8px;
  margin: 2px 6px;
  padding: 4px 0;
}
.sidebar-list row:selected {
  background: alpha(@accent_bg_color, 0.25);
}
.metric-value {
  font-weight: 700;
}
.dashboard-metric {
  font-size: 24px;
  font-weight: 700;
}
.dashboard-score {
  font-size: 34px;
  font-weight: 800;
}
.dashboard-card {
  padding: 14px 16px;
}
.dashboard-action {
  padding: 0;
  border-radius: 14px;
  background: alpha(@card_bg_color, 0.85);
  border: 1px solid alpha(@borders, 0.65);
}
.dashboard-action:hover {
  background: alpha(@accent_bg_color, 0.10);
}
.dashboard-action.quick-clean .action-icon {
  color: white;
  background: #d97706;
  border-radius: 12px;
}
.dashboard-action.full-check .action-icon {
  color: white;
  background: #2563eb;
  border-radius: 12px;
}
progressbar.score-good progress {
  background: #22c55e;
}
progressbar.score-warn progress {
  background: #f59e0b;
}
progressbar.score-bad progress {
  background: #ef4444;
}
"""


def _install_icon_search_paths() -> None:
    display = Gdk.Display.get_default()
    if display is None:
        return
    theme = Gtk.IconTheme.get_for_display(display)
    # portable: source tree next to package, user install, system theme
    pkg_root = Path(__file__).resolve().parents[1]
    candidates = [
        Path.home() / ".local/share/icons",
        Path.home() / ".icons",
        pkg_root / "data" / "icons",
        Path.home() / ".local/share/syscare/icons",
        Path("/usr/share/icons"),
    ]
    for p in candidates:
        if p.is_dir():
            try:
                theme.add_search_path(str(p))
            except Exception:  # noqa: BLE001
                pass


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: "SysCareApp"):
        super().__init__(application=app, title=__app_name__)
        self._app = app
        self.set_default_size(1100, 720)
        self.set_icon_name(ICON_NAME)

        self._toast = Adw.ToastOverlay()
        self.set_content(self._toast)

        toolbar = Adw.ToolbarView()
        self._toast.set_child(toolbar)

        header = Adw.HeaderBar()
        title = Adw.WindowTitle(title=__app_name__, subtitle=f"v{__version__} · system optimizer")
        header.set_title_widget(title)

        # Minimize to tray
        hide_btn = Gtk.Button(icon_name="window-minimize-symbolic")
        hide_btn.set_tooltip_text("Hide to tray")
        hide_btn.connect("clicked", lambda *_: self.hide_to_tray())
        header.pack_end(hide_btn)

        about_btn = Gtk.Button(icon_name="help-about-symbolic")
        about_btn.set_tooltip_text("About")
        about_btn.connect("clicked", self._about)
        header.pack_end(about_btn)
        toolbar.add_top_bar(header)

        paned = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        paned.set_hexpand(True)
        paned.set_vexpand(True)
        toolbar.set_content(paned)

        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar_box.set_size_request(220, -1)
        sidebar_box.add_css_class("sidebar-list")
        paned.append(sidebar_box)

        self._nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._nav.add_css_class("navigation-sidebar")
        self._nav.add_css_class("sidebar-list")
        self._nav.set_vexpand(True)
        self._nav.connect("row-selected", self._on_nav)
        nav_scroll = Gtk.ScrolledWindow(vexpand=True)
        nav_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        nav_scroll.set_child(self._nav)
        sidebar_box.append(nav_scroll)

        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)
        paned.append(self._stack)

        self._pages: dict[str, Gtk.Widget] = {}
        items = [
            ("dashboard", "user-home-symbolic", "Dashboard", DashboardPage),
            ("cleaner", "user-trash-symbolic", "System Cleaner", CleanerPage),
            ("security", "security-high-symbolic", "Security & Malware", SecurityPage),
            ("updates", "software-update-available-symbolic", "Software Updates", UpdaterPage),
            ("storage", "drive-harddisk-symbolic", "Storage Tools", StoragePage),
            ("network", "network-wired-symbolic", "Network", NetworkPage),
            ("processes", "application-x-executable-symbolic", "Processes", ProcessesPage),
            ("services", "emblem-system-symbolic", "Services", ServicesPage),
            ("startup", "system-run-symbolic", "Startup Apps", StartupPage),
            ("resources", "utilities-system-monitor-symbolic", "Resources", ResourcesPage),
            ("schedules", "alarm-symbolic", "Schedules", SchedulesPage),
            ("history", "document-open-recent-symbolic", "History", HistoryPage),
            ("uninstaller", "package-x-generic-symbolic", "Uninstaller", UninstallerPage),
        ]

        for page_id, icon, label, cls in items:
            row = Gtk.ListBoxRow()
            row.set_name(page_id)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(10)
            box.set_margin_end(10)
            img = Gtk.Image.new_from_icon_name(icon)
            lab = Gtk.Label(label=label, xalign=0, hexpand=True)
            box.append(img)
            box.append(lab)
            row.set_child(box)
            self._nav.append(row)

            if cls is DashboardPage:
                page = cls(toast=self._toast, navigate=self.show_page)
            else:
                page = cls(toast=self._toast)
            self._pages[page_id] = page
            self._stack.add_named(page, page_id)

        first = self._nav.get_row_at_index(0)
        if first:
            self._nav.select_row(first)

        # Close → tray (not quit)
        self.connect("close-request", self._on_close_request)

    def _on_nav(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        page_id = row.get_name()
        if page_id:
            self._stack.set_visible_child_name(page_id)

    def show_page(self, page_id: str) -> None:
        for i in range(64):
            row = self._nav.get_row_at_index(i)
            if row is None:
                break
            if row.get_name() == page_id:
                self._nav.select_row(row)
                break
        if page_id in self._pages:
            self._stack.set_visible_child_name(page_id)

    def hide_to_tray(self) -> None:
        tray = self._app._tray
        if tray is None or not tray.available:
            # no usable tray → real quit (avoid invisible zombie process)
            self._app._tray_quit()
            return
        self.set_visible(False)
        log.info("hidden to tray")

    def reveal_from_tray(self) -> None:
        self.set_visible(True)
        self.present()

    def _on_close_request(self, *_args) -> bool:
        """True = stop default close (keep app running in tray)."""
        self.hide_to_tray()
        return True

    def _about(self, *_args) -> None:
        dlg = Adw.AboutDialog(
            application_name=__app_name__,
            application_icon=ICON_NAME,
            version=__version__,
            developer_name="Bernard Bachmann",
            comments=(
                "Linux-native system maintenance, security, updates and monitoring.\n"
                "Close window or use minimize to keep the tray icon."
            ),
            website="file:///usr/share/doc/syscare/README.md",
            license_type=Gtk.License.GPL_3_0,
        )
        dlg.present(self)

    def dispose_pages(self) -> None:
        for p in self._pages.values():
            dispose = getattr(p, "dispose_timers", None)
            if callable(dispose):
                try:
                    dispose()
                except Exception:  # noqa: BLE001
                    pass


class SysCareApp(Adw.Application):
    def __init__(self, *, start_hidden: bool = False, no_tray: bool = False):
        super().__init__(application_id=__app_id__, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self._window: MainWindow | None = None
        self._tray: TrayIcon | None = None
        self._start_hidden = start_hidden
        self._no_tray = no_tray
        self._stats_timer = 0
        self._hold_token = None

    def do_startup(self) -> None:  # noqa: N802
        Adw.Application.do_startup(self)
        _install_icon_search_paths()
        css = Gtk.CssProvider()
        css.load_from_data(CSS.encode("utf-8"))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

        # Keep process alive when window is hidden (tray mode)
        self.hold()

        if not self._no_tray:
            self._tray = TrayIcon(
                on_show=self._tray_show,
                on_quit=self._tray_quit,
                on_toggle=self._tray_toggle,
                on_ready=self._on_tray_ready,
            )
            if self._tray.start():
                self._stats_timer = GLib.timeout_add_seconds(3, self._tick_stats)
                # if watcher never answers, fall back after a few seconds
                GLib.timeout_add_seconds(12, self._tray_ready_timeout)
            else:
                log.warning("tray unavailable")
                self._tray = None

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self._tray_quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<primary>q"])

    def do_activate(self) -> None:  # noqa: N802
        if self._window is None:
            self._window = MainWindow(self)
        if self._start_hidden and self._tray is not None:
            # Only hide when tray is actually registered with the watcher.
            # Until then show the window; _on_tray_ready may re-hide.
            if self._tray.available:
                self._window.set_visible(False)
                self._start_hidden = False
            else:
                self._window.present()
                # keep _start_hidden until tray ready or timeout
        else:
            self._window.present()

    def do_shutdown(self) -> None:  # noqa: N802
        if self._stats_timer:
            GLib.source_remove(self._stats_timer)
            self._stats_timer = 0
        if self._tray:
            self._tray.stop()
            self._tray = None
        if self._window:
            self._window.dispose_pages()
        Adw.Application.do_shutdown(self)

    def _on_tray_ready(self, ok: bool) -> None:
        if ok:
            log.info("tray ready for close-to-tray")
            # Honor deferred --tray / --start-hidden
            if self._start_hidden and self._window is not None:
                self._window.set_visible(False)
                self._start_hidden = False
        else:
            log.warning("tray not ready — close will quit instead of hide")
            if self._start_hidden and self._window is not None:
                # Keep window visible so the process is not invisible
                self._window.present()
                self._start_hidden = False

    def _tray_ready_timeout(self) -> bool:
        if self._tray is not None and not self._tray.available:
            log.warning("tray registration timeout — close-to-tray disabled")
            if self._start_hidden and self._window is not None:
                self._window.present()
                self._start_hidden = False
        return False

    def _tray_show(self) -> None:
        if self._window is None:
            self._window = MainWindow(self)
        self._window.reveal_from_tray()

    def _tray_toggle(self) -> None:
        if self._window is None:
            self._tray_show()
            return
        if self._window.is_visible():
            self._window.hide_to_tray()
        else:
            self._window.reveal_from_tray()

    def _tray_quit(self) -> None:
        if self._window:
            self._window.dispose_pages()
            # allow real close
            try:
                self._window.disconnect_by_func(self._window._on_close_request)
            except Exception:  # noqa: BLE001
                pass
            self._window.destroy()
            self._window = None
        if self._tray:
            self._tray.stop()
            self._tray = None
        self.release()
        self.quit()

    def _tick_stats(self) -> bool:
        if self._tray is None:
            return False

        def work() -> None:
            try:
                import psutil

                cpu = float(psutil.cpu_percent(interval=None))
                mem = psutil.virtual_memory()

                def ui() -> None:
                    if self._tray:
                        self._tray.update_stats(cpu, mem.percent, mem.used, mem.total)

                GLib.idle_add(ui)
            except Exception as e:  # noqa: BLE001
                log.debug("stats: %s", e)

        threading.Thread(target=work, daemon=True).start()
        return True


def run_app(
    argv: list[str] | None = None,
    *,
    start_hidden: bool = False,
    no_tray: bool = False,
) -> int:
    app = SysCareApp(start_hidden=start_hidden, no_tray=no_tray)
    return int(app.run(argv))
