"""GTK4 / Adw pages for SysCare."""

from __future__ import annotations

import logging
import threading
from collections import deque

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from . import cleaner, history, processes, services, startup, sysinfo, uninstaller
from .resources import ResourceMonitor
from .util import human_bytes, human_rate

log = logging.getLogger("syscare.pages")


def _spinner_page() -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
    box.set_hexpand(True)
    box.set_vexpand(True)
    spin = Gtk.Spinner(spinning=True)
    spin.set_size_request(32, 32)
    box.append(spin)
    box.append(Gtk.Label(label="Loading…"))
    return box


def _toast(host: Adw.ToastOverlay | None, msg: str) -> None:
    if host is None:
        return
    host.add_toast(Adw.Toast(title=msg, timeout=3))


def _confirm_destructive(
    parent: Gtk.Widget,
    *,
    heading: str,
    body: str,
    confirm_label: str,
    on_confirm,
) -> None:
    """Adw.AlertDialog with cancel + destructive confirm."""
    dlg = Adw.AlertDialog(heading=heading, body=body)
    dlg.add_response("cancel", "Cancel")
    dlg.add_response("ok", confirm_label)
    dlg.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
    dlg.set_default_response("cancel")
    dlg.set_close_response("cancel")

    def on_response(_d, response: str) -> None:
        if response == "ok":
            on_confirm()

    dlg.connect("response", on_response)
    root = parent.get_root()
    dlg.present(root if root is not None else parent)


class _PageBase:
    """Mixin: alive flag so background threads don't touch disposed widgets."""

    _alive: bool = True
    _timer: int = 0

    def _mark_alive(self) -> None:
        self._alive = True

    def _idle(self, fn) -> None:
        def wrap() -> bool:
            if not getattr(self, "_alive", False):
                return False
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("idle UI callback failed")
            return False

        GLib.idle_add(wrap)

    def dispose_timers(self) -> None:
        self._alive = False
        t = getattr(self, "_timer", 0) or 0
        if t:
            try:
                GLib.source_remove(t)
            except Exception:  # noqa: BLE001
                pass
            self._timer = 0


# ── Dashboard ──────────────────────────────────────────────────────────────


class DashboardPage(_PageBase, Gtk.Box):
    def __init__(self, toast: Adw.ToastOverlay | None = None, navigate=None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._mark_alive()
        self._toast = toast
        self._navigate = navigate or (lambda _page_id: None)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.append(self._scroll)

        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._content.set_margin_top(18)
        self._content.set_margin_bottom(24)
        self._content.set_margin_start(20)
        self._content.set_margin_end(20)
        self._scroll.set_child(self._content)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = Gtk.Label(label="Dashboard", xalign=0)
        title.add_css_class("title-1")
        subtitle = Gtk.Label(label="System overview and quick actions", xalign=0)
        subtitle.add_css_class("dim-label")
        heading.append(title)
        heading.append(subtitle)
        header.append(heading)
        refresh = Gtk.Button(label="Refresh", icon_name="view-refresh-symbolic")
        refresh.set_valign(Gtk.Align.CENTER)
        refresh.connect("clicked", lambda *_: self.refresh())
        header.append(refresh)
        self._content.append(header)

        # Kudu-style live gauges.
        self._cards = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._cards.set_homogeneous(True)
        self._content.append(self._cards)

        self._cpu_bar = self._metric_card("processor-symbolic", "CPU")
        self._mem_bar = self._metric_card("media-flash-symbolic", "Memory")
        self._disk_bar = self._metric_card("drive-harddisk-symbolic", "Disk")
        self._activity_bar = self._metric_card("emblem-ok-symbolic", "Care actions")
        self._cards.append(self._cpu_bar[0])
        self._cards.append(self._mem_bar[0])
        self._cards.append(self._disk_bar[0])
        self._cards.append(self._activity_bar[0])

        # Care score and current maintenance status.
        overview = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        overview.set_homogeneous(True)
        self._content.append(overview)

        score_card = self._card_box()
        score_title = Gtk.Label(label="System care score", xalign=0)
        score_title.add_css_class("title-4")
        self._score_value = Gtk.Label(label="—", xalign=0)
        self._score_value.add_css_class("dashboard-score")
        self._score_bar = Gtk.ProgressBar()
        self._score_detail = Gtk.Label(label="Calculating maintenance coverage…", xalign=0)
        self._score_detail.add_css_class("dim-label")
        self._score_detail.set_wrap(True)
        score_card.append(score_title)
        score_card.append(self._score_value)
        score_card.append(self._score_bar)
        score_card.append(self._score_detail)
        overview.append(score_card)

        status_card = self._card_box()
        status_title = Gtk.Label(label="Maintenance status", xalign=0)
        status_title.add_css_class("title-4")
        self._last_action_value = Gtk.Label(label="No action recorded", xalign=0)
        self._last_action_value.add_css_class("heading")
        self._last_action_detail = Gtk.Label(label="Run a cleaner or audit to get started", xalign=0)
        self._last_action_detail.add_css_class("dim-label")
        self._last_action_detail.set_ellipsize(Pango.EllipsizeMode.END)
        self._status_separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        self._uptime_status = Gtk.Label(label="Uptime —", xalign=0)
        self._uptime_status.add_css_class("caption")
        status_card.append(status_title)
        status_card.append(self._last_action_value)
        status_card.append(self._last_action_detail)
        status_card.append(self._status_separator)
        status_card.append(self._uptime_status)
        overview.append(status_card)

        # Lifetime maintenance statistics.
        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        stats.set_homogeneous(True)
        self._content.append(stats)
        self._reclaimed_stat = self._stat_card("Space reclaimed")
        self._clean_stat = self._stat_card("Clean runs")
        self._audit_stat = self._stat_card("Security checks")
        self._process_stat = self._stat_card("Processes")
        for card in (
            self._reclaimed_stat,
            self._clean_stat,
            self._audit_stat,
            self._process_stat,
        ):
            stats.append(card[0])

        # Quick actions mirror Kudu's dashboard, routed to SysCare's safe pages.
        actions_title = Gtk.Label(label="Quick actions", xalign=0)
        actions_title.add_css_class("title-3")
        self._content.append(actions_title)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        actions.set_homogeneous(True)
        actions.append(
            self._action_card(
                "user-trash-symbolic",
                "Quick Clean",
                "Review every cleaner category and choose what to remove",
                "cleaner",
                "quick-clean",
            )
        )
        actions.append(
            self._action_card(
                "security-high-symbolic",
                "Full System Check",
                "Audit security, malware protection and system hardening",
                "security",
                "full-check",
            )
        )
        self._content.append(actions)

        self._info_group = Adw.PreferencesGroup(title="System information")
        self._content.append(self._info_group)
        self._info_rows: list[Adw.ActionRow] = []

        self._disk_group = Adw.PreferencesGroup(title="Storage overview")
        self._content.append(self._disk_group)

        self._timer = GLib.timeout_add_seconds(3, self._tick)
        GLib.idle_add(self.refresh)

    def _card_box(self) -> Gtk.Box:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("card")
        card.add_css_class("dashboard-card")
        card.set_margin_top(2)
        card.set_margin_bottom(2)
        card.set_margin_start(2)
        card.set_margin_end(2)
        return card

    def _metric_card(
        self, icon_name: str, title: str
    ) -> tuple[Gtk.Box, Gtk.Label, Gtk.ProgressBar, Gtk.Label]:
        frame = self._card_box()
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.add_css_class("dim-label")
        t = Gtk.Label(label=title, xalign=0, hexpand=True)
        t.add_css_class("heading")
        title_row.append(icon)
        title_row.append(t)
        val = Gtk.Label(label="—", xalign=0)
        val.add_css_class("dashboard-metric")
        bar = Gtk.ProgressBar(show_text=False)
        bar.set_fraction(0)
        sub = Gtk.Label(label="", xalign=0)
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        frame.append(title_row)
        frame.append(val)
        frame.append(bar)
        frame.append(sub)
        return frame, val, bar, sub

    def _stat_card(self, title: str) -> tuple[Gtk.Box, Gtk.Label]:
        card = self._card_box()
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class("dim-label")
        label.add_css_class("caption")
        value = Gtk.Label(label="—", xalign=0)
        value.add_css_class("title-2")
        card.append(label)
        card.append(value)
        return card, value

    def _action_card(
        self,
        icon_name: str,
        title: str,
        description: str,
        page_id: str,
        style_class: str,
    ) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("dashboard-action")
        button.add_css_class(style_class)
        button.connect("clicked", lambda *_: self._navigate(page_id))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        row.set_margin_top(12)
        row.set_margin_bottom(12)
        row.set_margin_start(14)
        row.set_margin_end(14)
        icon_box = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        icon_box.add_css_class("action-icon")
        icon_box.set_size_request(48, 48)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(24)
        icon_box.append(icon)
        row.append(icon_box)
        copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        heading = Gtk.Label(label=title, xalign=0)
        heading.add_css_class("title-4")
        detail = Gtk.Label(label=description, xalign=0, wrap=True)
        detail.add_css_class("dim-label")
        detail.add_css_class("caption")
        copy.append(heading)
        copy.append(detail)
        row.append(copy)
        row.append(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        button.set_child(row)
        return button

    def _tick(self) -> bool:
        if not self._alive:
            return False
        self.refresh(quick=True)
        return True

    def refresh(self, quick: bool = False) -> None:
        if not self._alive:
            return

        def work() -> None:
            snap = sysinfo.collect(cpu_interval=0.12 if not quick else 0.0)
            self._idle(lambda: self._apply(snap))

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, s: sysinfo.SystemSnapshot) -> None:
        _, cpu_v, cpu_b, cpu_s = self._cpu_bar
        cpu_v.set_label(f"{s.cpu_percent:.0f}%")
        cpu_b.set_fraction(min(1.0, s.cpu_percent / 100.0))
        cpu_s.set_label(f"Load {s.load_avg[0]:.2f}")

        _, mem_v, mem_b, mem_s = self._mem_bar
        mem_v.set_label(f"{s.mem_percent:.0f}%")
        mem_b.set_fraction(min(1.0, s.mem_percent / 100.0))
        mem_s.set_label(f"{human_bytes(s.mem_used)} / {human_bytes(s.mem_total)}")

        disk_total = sum(d.total for d in s.disks)
        disk_used = sum(d.used for d in s.disks)
        disk_percent = (disk_used / disk_total * 100.0) if disk_total else 0.0
        _, disk_v, disk_b, disk_s = self._disk_bar
        disk_v.set_label(f"{disk_percent:.0f}%")
        disk_b.set_fraction(min(1.0, disk_percent / 100.0))
        disk_s.set_label(f"{human_bytes(disk_used)} / {human_bytes(disk_total)}")

        entries = history.list_entries()
        clean_runs = sum(1 for entry in entries if entry.action == "System clean")
        security_checks = sum(
            1
            for entry in entries
            if entry.action.startswith(("Security audit", "Malware scan"))
        )
        total_reclaimed = sum(entry.reclaimed for entry in entries)

        _, activity_v, activity_b, activity_s = self._activity_bar
        activity_v.set_label(str(len(entries)))
        activity_b.set_fraction(min(1.0, len(entries) / 10.0))
        activity_s.set_label("maintenance actions logged")

        self._reclaimed_stat[1].set_label(human_bytes(total_reclaimed))
        self._clean_stat[1].set_label(str(clean_runs))
        self._audit_stat[1].set_label(str(security_checks))
        self._process_stat[1].set_label(str(s.process_count))

        score = 100
        notes: list[str] = []
        if not entries:
            score -= 20
            notes.append("no maintenance history")
        if clean_runs == 0:
            score -= 10
            notes.append("cleaner not run yet")
        if security_checks == 0:
            score -= 10
            notes.append("security audit pending")
        recent_problems = sum(
            1 for entry in entries[:10] if entry.status in {"warning", "error"}
        )
        if recent_problems:
            score -= min(20, recent_problems * 4)
            notes.append(f"{recent_problems} recent warning(s)")
        if s.cpu_percent >= 90:
            score -= 10
            notes.append("high CPU load")
        if s.mem_percent >= 90:
            score -= 15
            notes.append("high memory use")
        if disk_percent >= 90:
            score -= 20
            notes.append("disk almost full")
        elif disk_percent >= 80:
            score -= 10
            notes.append("disk space getting low")
        score = max(0, min(100, score))
        self._score_value.set_label(f"{score}/100")
        self._score_bar.set_fraction(score / 100.0)
        for css_class in ("score-good", "score-warn", "score-bad"):
            self._score_bar.remove_css_class(css_class)
        self._score_bar.add_css_class(
            "score-good" if score >= 80 else "score-warn" if score >= 60 else "score-bad"
        )
        self._score_detail.set_label(
            "Well maintained · no current care warnings"
            if not notes
            else "Review: " + " · ".join(notes[:3])
        )

        if entries:
            last = entries[0]
            self._last_action_value.set_label(last.action)
            stamp = last.timestamp[:16].replace("T", " ")
            self._last_action_detail.set_label(f"{stamp} · {last.summary}")
        else:
            self._last_action_value.set_label("No action recorded")
            self._last_action_detail.set_label("Run a cleaner or audit to get started")
        self._uptime_status.set_label(
            f"Uptime {sysinfo.format_uptime(s.uptime_sec)} · {s.process_count} processes · "
            f"Swap {s.swap_percent:.0f}%"
        )

        # rebuild info rows
        while self._info_rows:
            row = self._info_rows.pop()
            self._info_group.remove(row)
        for k, v in sysinfo.summary_lines(s):
            if k in ("CPU usage", "Memory", "Swap", "Load avg"):
                continue
            row = Adw.ActionRow(title=k, subtitle=v)
            self._info_group.add(row)
            self._info_rows.append(row)

        # disks
        if not hasattr(self, "_disk_rows"):
            self._disk_rows: list[Adw.ActionRow] = []
        for r in self._disk_rows:
            self._disk_group.remove(r)
        self._disk_rows.clear()
        for d in s.disks:
            row = Adw.ActionRow(
                title=d.mount,
                subtitle=f"{d.device} · {d.fstype} · {human_bytes(d.used)} / {human_bytes(d.total)}",
            )
            bar = Gtk.ProgressBar()
            bar.set_fraction(min(1.0, d.percent / 100.0))
            bar.set_valign(Gtk.Align.CENTER)
            bar.set_size_request(120, -1)
            row.add_suffix(bar)
            pct = Gtk.Label(label=f"{d.percent:.0f}%")
            pct.add_css_class("dim-label")
            row.add_suffix(pct)
            self._disk_group.add(row)
            self._disk_rows.append(row)


# ── Cleaner ────────────────────────────────────────────────────────────────


class CleanerPage(_PageBase, Gtk.Box):
    def __init__(self, toast: Adw.ToastOverlay | None = None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._mark_alive()
        self._toast = toast
        self._targets: list[cleaner.CleanTarget] = []
        self._checks: dict[str, Gtk.CheckButton] = {}
        self._size_labels: dict[str, Gtk.Label] = {}
        self._title_labels: dict[str, Gtk.Label] = {}
        self._scanning = False

        toolbar = Gtk.Box(spacing=8)
        toolbar.set_margin_top(12)
        toolbar.set_margin_bottom(8)
        toolbar.set_margin_start(16)
        toolbar.set_margin_end(16)
        self.append(toolbar)

        self._scan_btn = Gtk.Button(label="Scan")
        self._scan_btn.add_css_class("suggested-action")
        self._scan_btn.connect("clicked", lambda *_: self.scan())
        toolbar.append(self._scan_btn)

        self._select_all_btn = Gtk.Button(label="Select all")
        self._select_all_btn.set_sensitive(False)
        self._select_all_btn.connect("clicked", lambda *_: self._set_all_selected(True))
        toolbar.append(self._select_all_btn)

        self._deselect_all_btn = Gtk.Button(label="Deselect all")
        self._deselect_all_btn.set_sensitive(False)
        self._deselect_all_btn.connect(
            "clicked", lambda *_: self._set_all_selected(False)
        )
        toolbar.append(self._deselect_all_btn)

        self._clean_btn = Gtk.Button(label="Clean selected")
        self._clean_btn.add_css_class("destructive-action")
        self._clean_btn.connect("clicked", lambda *_: self.clean())
        toolbar.append(self._clean_btn)

        self._spinner = Gtk.Spinner()
        self._spinner.set_visible(False)
        toolbar.append(self._spinner)

        self._status = Gtk.Label(label="Scanning…", xalign=0)
        self._status.add_css_class("dim-label")
        self._status.set_hexpand(True)
        self._status.set_ellipsize(Pango.EllipsizeMode.END)
        toolbar.append(self._status)

        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.append(scroll)
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._list.set_margin_start(16)
        self._list.set_margin_end(16)
        self._list.set_margin_bottom(16)
        self._list.set_vexpand(True)
        scroll.set_child(self._list)

        # Instant skeleton, then measure sizes
        GLib.idle_add(self.scan)

    def scan(self) -> None:
        if not self._alive:
            return
        if self._scanning:
            return
        self._scanning = True
        self._scan_btn.set_sensitive(False)
        self._spinner.set_visible(True)
        self._spinner.start()
        self._status.set_label("Scanning… listing targets")

        # Instant list so UI is never empty / stuck looking idle
        try:
            stubs = cleaner.scan_targets_structure_only()
            self._targets = stubs
            self._rebuild(measuring=True)
            self._status.set_label(f"Scanning… measuring {len(stubs)} targets")
        except Exception as e:  # noqa: BLE001
            log.exception("stub scan failed")
            self._status.set_label(f"Scan error: {e}")
            self._scan_done_ui()
            return

        def work() -> None:
            err: str | None = None
            try:
                for i, t in enumerate(self._targets):
                    if not self._alive:
                        return
                    cleaner.measure_target(t)
                    # progressive size updates

                    def ui_one(target=t, idx=i, total=len(self._targets)) -> None:
                        self._update_row_size(target)
                        self._status.set_label(
                            f"Scanning… {idx + 1}/{total}: {target.title}"
                        )

                    self._idle(ui_one)
            except Exception as e:  # noqa: BLE001
                log.exception("measure failed")
                err = str(e)

            def ui_done() -> None:
                self._scan_done_ui()
                if err:
                    self._status.set_label(f"Scan failed: {err[:120]}")
                    _toast(self._toast, "Scan failed")
                    return
                total = sum(t.size for t in self._targets if t.kind != "residual")
                residual = next((t for t in self._targets if t.kind == "residual"), None)
                extra = ""
                if residual and residual.size:
                    extra = f" · {residual.size} residual pkgs"
                self._status.set_label(
                    f"Scan done · ~{human_bytes(total)} reclaimable{extra}"
                )
                _toast(self._toast, f"Scan done · ~{human_bytes(total)}")
                # refresh residual title etc.
                self._rebuild(measuring=False)

            self._idle(ui_done)

        threading.Thread(target=work, daemon=True).start()

    def _scan_done_ui(self) -> None:
        self._scanning = False
        self._scan_btn.set_sensitive(True)
        self._spinner.stop()
        self._spinner.set_visible(False)

    def _update_row_size(self, t: cleaner.CleanTarget) -> None:
        sl = self._size_labels.get(t.id)
        if sl is None:
            return
        if t.kind == "residual":
            sl.set_label(f"{t.size} pkgs")
        else:
            sl.set_label(human_bytes(t.size))
        tl = self._title_labels.get(t.id)
        if tl is not None:
            tl.set_label(t.title)

    def _rebuild(self, *, measuring: bool = False) -> None:
        while True:
            row = self._list.get_row_at_index(0)
            if row is None:
                break
            self._list.remove(row)
        self._checks.clear()
        self._size_labels.clear()
        self._title_labels.clear()

        has_targets = bool(self._targets)
        self._select_all_btn.set_sensitive(has_targets)
        self._deselect_all_btn.set_sensitive(has_targets)

        for t in self._targets:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.set_margin_top(10)
            box.set_margin_bottom(10)
            box.set_margin_start(12)
            box.set_margin_end(12)
            cb = Gtk.CheckButton(active=t.selected)
            self._checks[t.id] = cb

            def on_toggled(btn: Gtk.CheckButton, target=t) -> None:
                target.selected = btn.get_active()

            cb.connect("toggled", on_toggled)
            box.append(cb)
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            title = Gtk.Label(label=t.title, xalign=0)
            title.add_css_class("heading")
            self._title_labels[t.id] = title
            sub = t.description or ""
            if t.needs_root:
                sub = f"[root] {sub}"
            sub = f"[{t.category} · {t.risk}] {sub}"
            desc = Gtk.Label(label=sub, xalign=0)
            desc.add_css_class("dim-label")
            desc.add_css_class("caption")
            desc.set_ellipsize(Pango.EllipsizeMode.END)
            col.append(title)
            col.append(desc)
            box.append(col)
            if measuring and t.size == 0 and t.kind not in ("residual",):
                size_txt = "…"
            elif t.kind == "residual":
                size_txt = f"{t.size} pkgs"
            else:
                size_txt = human_bytes(t.size)
            sl = Gtk.Label(label=size_txt)
            sl.add_css_class("numeric")
            sl.set_width_chars(10)
            sl.set_xalign(1.0)
            self._size_labels[t.id] = sl
            box.append(sl)
            row.set_child(box)
            self._list.append(row)

    def _set_all_selected(self, selected: bool) -> None:
        for target in self._targets:
            target.selected = selected
            check = self._checks.get(target.id)
            if check is not None and check.get_active() != selected:
                check.set_active(selected)

        selected_count = sum(1 for target in self._targets if target.selected)
        self._status.set_label(f"{selected_count}/{len(self._targets)} targets selected")

    def clean(self) -> None:
        selected = [t for t in self._targets if t.selected]
        if not selected:
            _toast(self._toast, "Nothing selected")
            return
        lines = []
        total = 0
        for t in selected:
            if t.kind == "residual":
                lines.append(f"• {t.title}")
            else:
                total += t.size
                root = " [root]" if t.needs_root else ""
                lines.append(f"• {t.title}{root} — {human_bytes(t.size)}")
        body = (
            "The following will be permanently deleted or cleaned:\n\n"
            + "\n".join(lines)
            + f"\n\nApprox. reclaimable: {human_bytes(total)}"
            + "\n\nRoot actions (if any) use a single password prompt."
        )

        def do_clean() -> None:
            self._run_clean(selected)

        _confirm_destructive(
            self,
            heading="Confirm system clean",
            body=body,
            confirm_label="Clean now",
            on_confirm=do_clean,
        )

    def _run_clean(self, selected: list[cleaner.CleanTarget]) -> None:
        if not self._alive:
            return
        self._clean_btn.set_sensitive(False)
        self._status.set_label("Cleaning… (may ask for password)")

        def work() -> None:
            results = cleaner.clean_targets(selected)

            def ui() -> None:
                freed = sum(r.freed for r in results)
                fails = [r for r in results if not r.ok]
                if fails:
                    _toast(self._toast, f"Done with errors · freed ~{human_bytes(freed)}")
                    self._status.set_label(fails[0].message[:120])
                else:
                    _toast(self._toast, f"Cleaned · freed ~{human_bytes(freed)}")
                    self._status.set_label(f"Done · freed ~{human_bytes(freed)}")
                history.add_entry(
                    "System clean",
                    "warning" if fails else "ok",
                    self._status.get_label(),
                    reclaimed=freed,
                    details={"targets": len(selected), "failures": len(fails)},
                )
                self._clean_btn.set_sensitive(True)
                self.scan()

            self._idle(ui)

        threading.Thread(target=work, daemon=True).start()


# ── Processes ──────────────────────────────────────────────────────────────


class ProcessesPage(_PageBase, Gtk.Box):
    def __init__(self, toast: Adw.ToastOverlay | None = None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._mark_alive()
        self._toast = toast
        self._rows: list[processes.ProcRow] = []
        self._filter = ""

        bar = Gtk.Box(spacing=8)
        bar.set_margin_top(12)
        bar.set_margin_bottom(8)
        bar.set_margin_start(16)
        bar.set_margin_end(16)
        self.append(bar)

        self._search = Gtk.SearchEntry(placeholder_text="Filter processes…")
        self._search.set_hexpand(True)
        self._search.connect("search-changed", self._on_search)
        bar.append(self._search)

        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda *_: self.refresh())
        bar.append(refresh)

        end = Gtk.Button(label="End")
        end.add_css_class("destructive-action")
        end.connect("clicked", lambda *_: self._kill(False))
        bar.append(end)

        kill = Gtk.Button(label="Kill")
        kill.connect("clicked", lambda *_: self._kill(True))
        bar.append(kill)

        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(scroll)

        self._model = Gtk.ListStore(int, str, str, str, str, str)  # pid name user cpu mem status
        self._filter_model = self._model.filter_new()
        self._filter_model.set_visible_func(self._visible)

        self._view = Gtk.TreeView(model=self._filter_model)
        self._view.set_headers_clickable(True)
        for i, title, expand in (
            (0, "PID", False),
            (1, "Name", True),
            (2, "User", False),
            (3, "CPU %", False),
            (4, "Mem %", False),
            (5, "Status", False),
        ):
            ren = Gtk.CellRendererText()
            if i in (3, 4, 0):
                ren.set_property("xalign", 1.0)
            col = Gtk.TreeViewColumn(title, ren, text=i)
            col.set_sort_column_id(i)
            col.set_resizable(True)
            col.set_expand(expand)
            self._view.append_column(col)
        scroll.set_child(self._view)

        self._timer = GLib.timeout_add_seconds(4, self._tick)
        GLib.idle_add(self.refresh)

    def _tick(self) -> bool:
        if not self._alive:
            return False
        self.refresh()
        return True

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self._filter = (entry.get_text() or "").lower()
        self._filter_model.refilter()

    def _visible(self, model, it, _data=None) -> bool:
        if not self._filter:
            return True
        name = model.get_value(it, 1) or ""
        user = model.get_value(it, 2) or ""
        pid = str(model.get_value(it, 0))
        return self._filter in name.lower() or self._filter in user.lower() or self._filter in pid

    def refresh(self) -> None:
        if not self._alive:
            return

        def work() -> None:
            rows = processes.list_processes()

            def ui() -> None:
                self._rows = rows
                self._model.clear()
                for r in rows:
                    self._model.append(
                        [
                            r.pid,
                            r.name,
                            r.user,
                            f"{r.cpu:.1f}",
                            f"{r.mem:.1f}",
                            r.status,
                        ]
                    )

            self._idle(ui)

        threading.Thread(target=work, daemon=True).start()

    def _selected_pid(self) -> int | None:
        sel = self._view.get_selection()
        model, it = sel.get_selected()
        if it is None:
            return None
        return int(model.get_value(it, 0))

    def _kill(self, force: bool) -> None:
        pid = self._selected_pid()
        if pid is None:
            _toast(self._toast, "Select a process")
            return
        label = "Kill" if force else "End"
        _confirm_destructive(
            self,
            heading=f"{label} process {pid}?",
            body=f"Send {'SIGKILL' if force else 'SIGTERM'} to PID {pid}.",
            confirm_label=label,
            on_confirm=lambda: self._do_kill(pid, force),
        )

    def _do_kill(self, pid: int, force: bool) -> None:
        ok, msg = processes.kill_process(pid, force=force)
        _toast(self._toast, msg)
        if ok:
            self.refresh()


# ── Services ───────────────────────────────────────────────────────────────


class ServicesPage(_PageBase, Gtk.Box):
    def __init__(self, toast: Adw.ToastOverlay | None = None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._mark_alive()
        self._toast = toast
        self._user_mode = False
        self._services: list[services.ServiceRow] = []
        self._filter = ""

        bar = Gtk.Box(spacing=8)
        bar.set_margin_top(12)
        bar.set_margin_bottom(8)
        bar.set_margin_start(16)
        bar.set_margin_end(16)
        self.append(bar)

        self._search = Gtk.SearchEntry(placeholder_text="Filter services…")
        self._search.set_hexpand(True)
        self._search.connect("search-changed", self._on_search)
        bar.append(self._search)

        self._user_sw = Gtk.CheckButton(label="User units")
        self._user_sw.connect("toggled", self._on_user)
        bar.append(self._user_sw)

        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda *_: self.refresh())
        bar.append(refresh)

        for label, action in (
            ("Start", "start"),
            ("Stop", "stop"),
            ("Restart", "restart"),
            ("Enable", "enable"),
            ("Disable", "disable"),
        ):
            b = Gtk.Button(label=label)
            if action == "stop":
                b.add_css_class("destructive-action")
            b.connect("clicked", lambda _b, a=action: self._act(a))
            bar.append(b)

        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(scroll)
        self._model = Gtk.ListStore(str, str, str, str, str)  # name active sub state desc
        self._fmodel = self._model.filter_new()
        self._fmodel.set_visible_func(self._visible)
        self._view = Gtk.TreeView(model=self._fmodel)
        for i, title, expand in (
            (0, "Unit", True),
            (1, "Active", False),
            (2, "Sub", False),
            (3, "Enabled", False),
            (4, "Description", True),
        ):
            ren = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(title, ren, text=i)
            col.set_resizable(True)
            col.set_expand(expand)
            col.set_sort_column_id(i)
            self._view.append_column(col)
        scroll.set_child(self._view)

        self._err = Gtk.Label(label="", xalign=0, wrap=True)
        self._err.add_css_class("error")
        self._err.set_margin_start(16)
        self._err.set_margin_end(16)
        self._err.set_margin_bottom(8)
        self._err.set_visible(False)
        self.append(self._err)

        GLib.idle_add(self.refresh)

    def _on_user(self, btn: Gtk.CheckButton) -> None:
        self._user_mode = btn.get_active()
        self.refresh()

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self._filter = (entry.get_text() or "").lower()
        self._fmodel.refilter()

    def _visible(self, model, it, _data=None) -> bool:
        if not self._filter:
            return True
        blob = " ".join(str(model.get_value(it, i) or "") for i in range(5)).lower()
        return self._filter in blob

    def refresh(self) -> None:
        if not self._alive:
            return
        user = self._user_mode

        def work() -> None:
            result = services.list_services(user=user)

            def ui() -> None:
                self._services = result.rows
                self._model.clear()
                for s in result.rows:
                    self._model.append(
                        [s.name, s.active, s.sub, s.unit_file_state or "—", s.description]
                    )
                if result.error:
                    prefix = "Warning: " if result.rows else "Could not list services: "
                    self._err.set_label(f"{prefix}{result.error}")
                    self._err.set_visible(True)
                    if not result.rows:
                        _toast(self._toast, "Services: error (see banner)")
                else:
                    self._err.set_visible(False)
                    self._err.set_label("")

            self._idle(ui)

        threading.Thread(target=work, daemon=True).start()

    def _selected(self) -> str | None:
        sel = self._view.get_selection()
        model, it = sel.get_selected()
        if it is None:
            return None
        return str(model.get_value(it, 0))

    def _act(self, action: str) -> None:
        name = self._selected()
        if not name:
            _toast(self._toast, "Select a service")
            return

        def work() -> None:
            ok, msg = services.service_action(name, action, user=self._user_mode)

            def ui() -> None:
                _toast(self._toast, msg if ok else f"Error: {msg[:100]}")
                self.refresh()

            self._idle(ui)

        threading.Thread(target=work, daemon=True).start()


# ── Startup ────────────────────────────────────────────────────────────────


class StartupPage(_PageBase, Gtk.Box):
    def __init__(self, toast: Adw.ToastOverlay | None = None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._mark_alive()
        self._toast = toast
        self._apps: list[startup.StartupApp] = []

        bar = Gtk.Box(spacing=8)
        bar.set_margin_top(12)
        bar.set_margin_bottom(8)
        bar.set_margin_start(16)
        bar.set_margin_end(16)
        self.append(bar)
        refresh = Gtk.Button(label="Refresh")
        refresh.add_css_class("suggested-action")
        refresh.connect("clicked", lambda *_: self.refresh())
        bar.append(refresh)
        self._status = Gtk.Label(label="", xalign=0, hexpand=True)
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
        GLib.idle_add(self.refresh)

    def refresh(self) -> None:
        if not self._alive:
            return

        def work() -> None:
            apps = startup.list_startup_apps()

            def ui() -> None:
                self._apps = apps
                while True:
                    row = self._list.get_row_at_index(0)
                    if row is None:
                        break
                    self._list.remove(row)
                enabled_n = sum(1 for a in apps if a.enabled)
                self._status.set_label(f"{enabled_n} enabled · {len(apps)} total")
                for app in apps:
                    row = Adw.ActionRow(title=app.name, subtitle=app.exec_cmd or app.comment or str(app.path))
                    if app.system:
                        badge = Gtk.Label(label="system")
                        badge.add_css_class("dim-label")
                        badge.add_css_class("caption")
                        row.add_suffix(badge)
                    sw = Gtk.Switch(active=app.enabled, valign=Gtk.Align.CENTER)
                    sw.connect("notify::active", self._on_toggle, app)
                    row.add_suffix(sw)
                    row.set_activatable_widget(sw)
                    self._list.append(row)

            self._idle(ui)

        threading.Thread(target=work, daemon=True).start()

    def _on_toggle(self, sw: Gtk.Switch, _pspec, app: startup.StartupApp) -> None:
        enabled = sw.get_active()

        def work() -> None:
            ok, msg = startup.set_startup_enabled(app, enabled)

            def ui() -> None:
                if ok:
                    app.enabled = enabled
                    _toast(self._toast, "Startup updated")
                else:
                    sw.set_active(not enabled)
                    _toast(self._toast, f"Failed: {msg}")

            self._idle(ui)

        threading.Thread(target=work, daemon=True).start()


# ── Resources ──────────────────────────────────────────────────────────────


class GraphArea(Gtk.DrawingArea):
    def __init__(self, title: str, color: tuple[float, float, float], history: deque, attr: str, percent: bool = True):
        super().__init__()
        self._title = title
        self._color = color
        self._history = history
        self._attr = attr
        self._percent = percent
        self.set_content_height(120)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def _draw(self, area, cr, width: int, height: int) -> None:
        cr.set_source_rgb(0.12, 0.14, 0.18)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        # grid
        cr.set_source_rgba(1, 1, 1, 0.06)
        for i in range(1, 4):
            y = height * i / 4
            cr.move_to(0, y)
            cr.line_to(width, y)
            cr.stroke()

        hist = list(self._history)
        if len(hist) < 2:
            return
        values = [float(getattr(s, self._attr)) for s in hist]
        if self._percent:
            vmax = 100.0
        else:
            vmax = max(values) or 1.0
            vmax = max(vmax, 1.0)

        r, g, b = self._color
        cr.set_source_rgba(r, g, b, 0.85)
        cr.set_line_width(2.0)
        n = len(values)
        for i, v in enumerate(values):
            x = i * (width - 1) / max(1, n - 1)
            y = height - (min(v, vmax) / vmax) * (height - 4) - 2
            if i == 0:
                cr.move_to(x, y)
            else:
                cr.line_to(x, y)
        cr.stroke()

        # fill
        cr.set_source_rgba(r, g, b, 0.15)
        cr.move_to(0, height)
        for i, v in enumerate(values):
            x = i * (width - 1) / max(1, n - 1)
            y = height - (min(v, vmax) / vmax) * (height - 4) - 2
            cr.line_to(x, y)
        cr.line_to(width, height)
        cr.close_path()
        cr.fill()


class ResourcesPage(_PageBase, Gtk.Box):
    def __init__(self, toast: Adw.ToastOverlay | None = None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._mark_alive()
        self._toast = toast
        self._mon = ResourceMonitor()
        # prime cpu
        import psutil

        psutil.cpu_percent(None)

        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(16)
        self.set_margin_end(16)

        self._labels: dict[str, Gtk.Label] = {}
        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(scroll)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        scroll.set_child(col)

        for key, title, color, attr, percent in (
            ("cpu", "CPU", (0.35, 0.65, 1.0), "cpu", True),
            ("mem", "Memory", (0.30, 0.80, 0.45), "mem", True),
            ("net_down", "Network download", (0.95, 0.70, 0.25), "net_down", False),
            ("net_up", "Network upload", (0.90, 0.40, 0.40), "net_up", False),
            ("disk_r", "Disk read", (0.60, 0.50, 0.95), "disk_read", False),
            ("disk_w", "Disk write", (0.85, 0.45, 0.75), "disk_write", False),
        ):
            head = Gtk.Box(spacing=8)
            t = Gtk.Label(label=title, xalign=0, hexpand=True)
            t.add_css_class("heading")
            val = Gtk.Label(label="—")
            val.add_css_class("numeric")
            self._labels[key] = val
            head.append(t)
            head.append(val)
            col.append(head)
            graph = GraphArea(title, color, self._mon.history, attr, percent=percent)
            col.append(graph)
            setattr(self, f"_graph_{key}", graph)

        self._timer = GLib.timeout_add(1000, self._tick)

    def _tick(self) -> bool:
        if not self._alive:
            return False
        s = self._mon.sample()
        self._labels["cpu"].set_label(f"{s.cpu:.0f}%")
        self._labels["mem"].set_label(f"{s.mem:.0f}%")
        self._labels["net_down"].set_label(human_rate(s.net_down))
        self._labels["net_up"].set_label(human_rate(s.net_up))
        self._labels["disk_r"].set_label(human_rate(s.disk_read))
        self._labels["disk_w"].set_label(human_rate(s.disk_write))
        for key in ("cpu", "mem", "net_down", "net_up", "disk_r", "disk_w"):
            getattr(self, f"_graph_{key}").queue_draw()
        return True


# ── Uninstaller ────────────────────────────────────────────────────────────


class UninstallerPage(_PageBase, Gtk.Box):
    def __init__(self, toast: Adw.ToastOverlay | None = None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._mark_alive()
        self._toast = toast
        self._pkgs: list[uninstaller.PackageRow] = []
        self._filter = ""

        bar = Gtk.Box(spacing=8)
        bar.set_margin_top(12)
        bar.set_margin_bottom(8)
        bar.set_margin_start(16)
        bar.set_margin_end(16)
        self.append(bar)

        self._search = Gtk.SearchEntry(placeholder_text="Search packages…")
        self._search.set_hexpand(True)
        self._search.connect("search-changed", self._on_search)
        bar.append(self._search)

        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda *_: self.refresh())
        bar.append(refresh)

        self._purge = Gtk.CheckButton(label="Also remove unused deps (-Rns)")
        bar.append(self._purge)

        self._remove_btn = Gtk.Button(label="Remove selected")
        self._remove_btn.add_css_class("destructive-action")
        self._remove_btn.connect("clicked", lambda *_: self._remove())
        bar.append(self._remove_btn)

        self._status = Gtk.Label(label="", xalign=0)
        self._status.add_css_class("dim-label")
        bar.append(self._status)

        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(scroll)
        self._model = Gtk.ListStore(bool, str, str, str, str)  # sel name ver size desc
        self._fmodel = self._model.filter_new()
        self._fmodel.set_visible_func(self._visible)
        self._view = Gtk.TreeView(model=self._fmodel)
        # checkbox column
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._on_toggled)
        col0 = Gtk.TreeViewColumn("", toggle, active=0)
        self._view.append_column(col0)
        for i, title, expand in (
            (1, "Package", True),
            (2, "Version", False),
            (3, "Size", False),
            (4, "Description", True),
        ):
            ren = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(title, ren, text=i)
            col.set_resizable(True)
            col.set_expand(expand)
            col.set_sort_column_id(i)
            self._view.append_column(col)
        scroll.set_child(self._view)
        GLib.idle_add(self.refresh)

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self._filter = (entry.get_text() or "").lower()
        self._fmodel.refilter()

    def _visible(self, model, it, _data=None) -> bool:
        if not self._filter:
            return True
        name = (model.get_value(it, 1) or "").lower()
        desc = (model.get_value(it, 4) or "").lower()
        return self._filter in name or self._filter in desc

    def _on_toggled(self, _renderer, path_str: str) -> None:
        # path is in filter model
        fit = self._fmodel.get_iter(path_str)
        child_path = self._fmodel.convert_path_to_child_path(self._fmodel.get_path(fit))
        it = self._model.get_iter(child_path)
        cur = self._model.get_value(it, 0)
        self._model.set_value(it, 0, not cur)

    def refresh(self) -> None:
        if not self._alive:
            return
        self._status.set_label("Loading packages…")

        def work() -> None:
            pkgs = uninstaller.list_packages()

            def ui() -> None:
                self._pkgs = pkgs
                self._model.clear()
                for p in pkgs:
                    self._model.append(
                        [
                            False,
                            p.name,
                            p.version,
                            human_bytes(p.size),
                            p.description,
                        ]
                    )
                self._status.set_label(f"{len(pkgs)} packages")

            self._idle(ui)

        threading.Thread(target=work, daemon=True).start()

    def _selected_names(self) -> list[str]:
        names: list[str] = []
        it = self._model.get_iter_first()
        while it is not None:
            if self._model.get_value(it, 0):
                names.append(str(self._model.get_value(it, 1)))
            it = self._model.iter_next(it)
        return names

    def _remove(self) -> None:
        names = self._selected_names()
        if not names:
            _toast(self._toast, "Select packages to remove")
            return
        purge = self._purge.get_active()
        self._status.set_label("Simulating pacman transaction…")
        self._clean_busy(True)

        def work() -> None:
            plan = uninstaller.simulate_remove(names, purge=purge)

            def ui() -> None:
                self._clean_busy(False)
                if not plan.ok:
                    _toast(self._toast, f"Simulate failed: {plan.error[:100]}")
                    self._status.set_label(plan.error[:120] or "simulate failed")
                    return
                body = (
                    f"Packages: {', '.join(names[:12])}{'…' if len(names) > 12 else ''}\n"
                    f"Action: pacman {plan.action}\n\n"
                    f"Transaction preview (pacman --print):\n{plan.summary[:1800]}"
                    "\n\nThis may require your password (pkexec)."
                )

                def do_remove() -> None:
                    self._run_remove(names, purge)

                _confirm_destructive(
                    self,
                    heading=f"Confirm package {plan.action}",
                    body=body,
                    confirm_label=plan.action.capitalize(),
                    on_confirm=do_remove,
                )
                self._status.set_label("Waiting for confirmation…")

            self._idle(ui)

        threading.Thread(target=work, daemon=True).start()

    def _clean_busy(self, busy: bool) -> None:
        btn = getattr(self, "_remove_btn", None)
        if btn is not None:
            btn.set_sensitive(not busy)

    def _run_remove(self, names: list[str], purge: bool) -> None:
        if not self._alive:
            return
        self._status.set_label(f"Removing {len(names)} package(s)…")
        self._clean_busy(True)

        def work() -> None:
            ok, msg = uninstaller.remove_packages(names, purge=purge)

            def ui() -> None:
                self._clean_busy(False)
                _toast(self._toast, msg if ok else f"Error: {msg[:120]}")
                self._status.set_label(msg[:80])
                self.refresh()

            self._idle(ui)

        threading.Thread(target=work, daemon=True).start()
