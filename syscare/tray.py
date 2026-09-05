"""System tray via StatusNotifierItem (GNOME AppIndicator-compatible).

Pure Gio/DBus — works inside the GTK4 process (AyatanaAppIndicator needs GTK3
and cannot load alongside Gtk 4.0).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib

from . import __app_id__, __app_name__, __version__
from .util import human_bytes

log = logging.getLogger("syscare.tray")

SNI_IFACE = "org.kde.StatusNotifierItem"
WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
WATCHER_IFACE = "org.kde.StatusNotifierWatcher"
MENU_IFACE = "com.canonical.dbusmenu"


def ensure_tray_pixmap() -> Path | None:
    """Install a 32px icon at ~/.local/share/pixmaps/syscare.png (no spaces)."""
    dest_dir = Path.home() / ".local/share/pixmaps"
    dest = dest_dir / "syscare.png"
    root = Path(__file__).resolve().parents[1]
    srcs = [
        Path.home() / ".local/share/icons/hicolor/32x32/apps/com.bbachmann.syscare.png",
        Path.home() / ".local/share/icons/hicolor/32x32/apps/syscare.png",
        Path("/usr/share/icons/hicolor/32x32/apps/com.bbachmann.syscare.png"),
        Path("/usr/share/icons/hicolor/32x32/apps/syscare.png"),
        Path("/usr/share/icons/hicolor/48x48/apps/com.bbachmann.syscare.png"),
        root / "data/icons/syscare.svg",
        Path("/usr/share/icons/hicolor/scalable/apps/com.bbachmann.syscare.svg"),
    ]
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    for src in srcs:
        if not src.is_file():
            continue
        try:
            if src.suffix.lower() == ".png":
                if not dest.is_file() or dest.stat().st_mtime < src.stat().st_mtime:
                    shutil.copy2(src, dest)
                return dest
        except OSError as e:
            log.debug("pixmap copy: %s", e)

    svg = next((p for p in srcs if p.suffix.lower() == ".svg" and p.is_file()), None)
    if svg is not None and not dest.is_file():
        for cmd in (
            ["magick", "-background", "none", str(svg), "-resize", "32x32", str(dest)],
            ["convert", "-background", "none", str(svg), "-resize", "32x32", str(dest)],
        ):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=15)
                if r.returncode == 0 and dest.is_file():
                    return dest
            except Exception:  # noqa: BLE001
                continue
    return dest if dest.is_file() else None


def _resolve_icon() -> tuple[str, str]:
    """Return an icon path the StatusNotifierItem host can resolve directly."""
    user_pixmap = ensure_tray_pixmap()
    direct_paths = [
        Path("/usr/share/pixmaps/com.bbachmann.syscare.png"),
        Path("/usr/share/pixmaps/syscare.png"),
        user_pixmap,
    ]
    for path in direct_paths:
        if path is not None and path.is_file() and " " not in str(path):
            # Absolute IconName is accepted by StatusNotifierItem hosts.
            return str(path), str(path.parent)

    theme_paths = [
        Path.home() / ".local/share/icons",
        Path("/usr/share/icons"),
        Path.home() / ".local/share/pixmaps",
        Path("/usr/share/pixmaps"),
    ]
    for base in theme_paths:
        if base.is_dir() and " " not in str(base):
            # hicolor layout
            if (base / "hicolor/32x32/apps/com.bbachmann.syscare.png").is_file() or (
                base / "hicolor/scalable/apps/com.bbachmann.syscare.svg"
            ).is_file():
                return __app_id__, str(base)
            if (base / "syscare.png").is_file():
                return "syscare", str(base)
    # last resort absolute path (some hosts accept it)
    pixmap = Path.home() / ".local/share/pixmaps/syscare.png"
    if pixmap.is_file() and " " not in str(pixmap):
        return str(pixmap), str(pixmap.parent)
    return __app_id__, ""


def _icon_source() -> Path | None:
    """Find the best raster/vector source for the SNI pixmap fallback."""
    root = Path(__file__).resolve().parents[1]
    candidates = [
        Path("/usr/share/icons/hicolor/scalable/apps/com.bbachmann.syscare.svg"),
        Path("/usr/share/icons/hicolor/128x128/apps/com.bbachmann.syscare.png"),
        root / "data/icons/syscare.svg",
        Path.home()
        / ".local/share/icons/hicolor/scalable/apps/com.bbachmann.syscare.svg",
        Path.home() / ".local/share/pixmaps/syscare.png",
    ]
    return next((path for path in candidates if path.is_file()), None)


def _pixbuf_to_argb(pixbuf: GdkPixbuf.Pixbuf) -> bytes:
    """Convert GdkPixbuf RGBA bytes to StatusNotifierItem ARGB32 bytes."""
    pixels = bytes(pixbuf.get_pixels())
    channels = pixbuf.get_n_channels()
    rowstride = pixbuf.get_rowstride()
    has_alpha = pixbuf.get_has_alpha()
    out = bytearray()
    for y in range(pixbuf.get_height()):
        row = y * rowstride
        for x in range(pixbuf.get_width()):
            offset = row + x * channels
            red, green, blue = pixels[offset : offset + 3]
            alpha = pixels[offset + 3] if has_alpha else 255
            out.extend((alpha, red, green, blue))
    return bytes(out)


def _load_icon_pixmaps() -> list[tuple[int, int, bytes]]:
    """Embed multiple icon sizes so panels never depend on theme lookup."""
    source = _icon_source()
    if source is None:
        log.warning("tray icon source not found; IconPixmap unavailable")
        return []

    pixmaps: list[tuple[int, int, bytes]] = []
    for size in (16, 22, 24, 32, 48):
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(source), size, size, True
            )
            pixmaps.append(
                (pixbuf.get_width(), pixbuf.get_height(), _pixbuf_to_argb(pixbuf))
            )
        except GLib.Error as error:
            log.warning("tray pixmap %spx failed: %s", size, error)
    return pixmaps


def _props_map(props: dict) -> dict[str, GLib.Variant]:
    out: dict[str, GLib.Variant] = {}
    for k, v in props.items():
        if isinstance(v, bool):
            out[k] = GLib.Variant("b", v)
        else:
            out[k] = GLib.Variant("s", str(v))
    return out


def _menu_node(iid: int, props: dict, children: list[GLib.Variant]) -> GLib.Variant:
    # (ia{sv}av) — children are variants of nested layout nodes
    return GLib.Variant("(ia{sv}av)", (iid, _props_map(props), children))


class TrayIcon:
    def __init__(
        self,
        *,
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
        on_toggle: Callable[[], None] | None = None,
        on_ready: Callable[[bool], None] | None = None,
    ):
        self._on_show = on_show
        self._on_quit = on_quit
        self._on_toggle = on_toggle or on_show
        self._on_ready = on_ready
        self._conn: Gio.DBusConnection | None = None
        self._reg_ids: list[int] = []
        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._item_path = "/StatusNotifierItem"
        self._menu_path = "/StatusNotifierItem/menu"
        self._status = "Active"
        self._tooltip = f"{__app_name__} {__version__}"
        self._label_cpu = "CPU/RAM …"
        self._owner_id = 0
        self._alive = False
        self._registered = False
        self._revision = 1
        self._icon_name, self._icon_theme = _resolve_icon()
        self._icon_pixmaps = _load_icon_pixmaps()

    @property
    def available(self) -> bool:
        """True only after StatusNotifierWatcher accepted registration."""
        return self._registered and self._alive

    def start(self) -> bool:
        """Begin bus ownership. Tray is usable only after available becomes True."""
        try:
            self._owner_id = Gio.bus_own_name(
                Gio.BusType.SESSION,
                self._bus_name,
                Gio.BusNameOwnerFlags.NONE,
                self._on_bus_acquired,
                self._on_name_acquired,
                self._on_name_lost,
            )
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("tray start failed: %s", e)
            return False

    def stop(self) -> None:
        self._alive = False
        for rid in self._reg_ids:
            if self._conn is not None:
                try:
                    self._conn.unregister_object(rid)
                except Exception:  # noqa: BLE001
                    pass
        self._reg_ids.clear()
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0
        self._conn = None

    def set_tooltip(self, text: str) -> None:
        self._tooltip = text
        self._emit_sni("NewToolTip")

    def update_stats(self, cpu: float, mem_percent: float, mem_used: int, mem_total: int) -> None:
        self._label_cpu = (
            f"CPU {cpu:.0f}% · RAM {mem_percent:.0f}% "
            f"({human_bytes(mem_used)} / {human_bytes(mem_total)})"
        )
        self.set_tooltip(f"{__app_name__}\n{self._label_cpu}")
        self._revision += 1
        self._emit_menu_layout()

    # ── bus ────────────────────────────────────────────────────────────

    def _on_bus_acquired(self, conn: Gio.DBusConnection, _name: str) -> None:
        self._conn = conn
        try:
            # register_object wants DBusInterfaceInfo, not the full node
            sni_iface = self._sni_info().interfaces[0]
            menu_iface = self._menu_info().interfaces[0]
            self._reg_ids.append(
                conn.register_object(
                    self._item_path,
                    sni_iface,
                    self._sni_method,
                    self._sni_get_prop,
                    None,
                )
            )
            self._reg_ids.append(
                conn.register_object(
                    self._menu_path,
                    menu_iface,
                    self._menu_method,
                    self._menu_get_prop,
                    None,
                )
            )
        except Exception as e:  # noqa: BLE001
            log.warning("register tray objects failed: %s", e)

    def _on_name_acquired(self, conn: Gio.DBusConnection, _name: str) -> None:
        self._alive = True
        self._register_with_watcher(conn)
        GLib.timeout_add_seconds(2, self._retry_register)
        GLib.timeout_add_seconds(10, self._retry_register)

    def _on_name_lost(self, _conn: Gio.DBusConnection, _name: str) -> None:
        log.warning("tray bus name lost")
        self._alive = False
        self._registered = False
        if self._on_ready:
            GLib.idle_add(self._on_ready, False)

    def _retry_register(self) -> bool:
        if self._conn and self._alive and not self._registered:
            self._register_with_watcher(self._conn)
        return False

    def _register_with_watcher(self, conn: Gio.DBusConnection) -> None:
        try:
            conn.call(
                WATCHER,
                WATCHER_PATH,
                WATCHER_IFACE,
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (self._bus_name,)),
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
                self._on_registered,
            )
        except Exception as e:  # noqa: BLE001
            log.debug("RegisterStatusNotifierItem: %s", e)
            if self._on_ready:
                GLib.idle_add(self._on_ready, False)

    def _on_registered(self, conn: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
        try:
            conn.call_finish(result)
            self._registered = True
            log.info("tray registered (StatusNotifierItem)")
            if self._on_ready:
                GLib.idle_add(self._on_ready, True)
        except Exception as e:  # noqa: BLE001
            self._registered = False
            log.warning(
                "tray not shown — enable GNOME extension "
                "'AppIndicator and KStatusNotifierItem Support': %s",
                e,
            )
            if self._on_ready:
                GLib.idle_add(self._on_ready, False)

    # ── SNI ────────────────────────────────────────────────────────────

    def _sni_info(self) -> Gio.DBusNodeInfo:
        return Gio.DBusNodeInfo.new_for_xml(
            f"""
            <node>
              <interface name="{SNI_IFACE}">
                <method name="ContextMenu">
                  <arg type="i" name="x" direction="in"/>
                  <arg type="i" name="y" direction="in"/>
                </method>
                <method name="Activate">
                  <arg type="i" name="x" direction="in"/>
                  <arg type="i" name="y" direction="in"/>
                </method>
                <method name="SecondaryActivate">
                  <arg type="i" name="x" direction="in"/>
                  <arg type="i" name="y" direction="in"/>
                </method>
                <method name="Scroll">
                  <arg type="i" name="delta" direction="in"/>
                  <arg type="s" name="orientation" direction="in"/>
                </method>
                <property name="Category" type="s" access="read"/>
                <property name="Id" type="s" access="read"/>
                <property name="Title" type="s" access="read"/>
                <property name="Status" type="s" access="read"/>
                <property name="WindowId" type="u" access="read"/>
                <property name="IconName" type="s" access="read"/>
                <property name="IconThemePath" type="s" access="read"/>
                <property name="OverlayIconName" type="s" access="read"/>
                <property name="AttentionIconName" type="s" access="read"/>
                <property name="AttentionMovieName" type="s" access="read"/>
                <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
                <property name="ItemIsMenu" type="b" access="read"/>
                <property name="Menu" type="o" access="read"/>
                <property name="IconPixmap" type="a(iiay)" access="read"/>
                <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
                <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
                <signal name="NewTitle"/>
                <signal name="NewIcon"/>
                <signal name="NewAttentionIcon"/>
                <signal name="NewOverlayIcon"/>
                <signal name="NewToolTip"/>
                <signal name="NewStatus">
                  <arg type="s" name="status"/>
                </signal>
              </interface>
            </node>
            """
        )

    def _sni_method(
        self,
        _conn,
        _sender,
        _path,
        _iface,
        method: str,
        _params,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self._on_toggle)
            invocation.return_value(None)
            return
        if method in ("ContextMenu", "Scroll"):
            invocation.return_value(None)
            return
        invocation.return_error_literal(
            Gio.dbus_error_quark(),
            Gio.DBusError.UNKNOWN_METHOD,
            f"Unknown method {method}",
        )

    def _sni_get_prop(self, _conn, _sender, _path, _iface, name: str):
        mapping = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "syscare"),
            "Title": GLib.Variant("s", __app_name__),
            "Status": GLib.Variant("s", self._status),
            "WindowId": GLib.Variant("u", 0),
            "IconName": GLib.Variant("s", self._icon_name),
            "IconThemePath": GLib.Variant("s", self._icon_theme),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "AttentionMovieName": GLib.Variant("s", ""),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", self._menu_path),
            "IconPixmap": GLib.Variant("a(iiay)", self._icon_pixmaps),
            "AttentionIconPixmap": GLib.Variant("a(iiay)", []),
            "OverlayIconPixmap": GLib.Variant("a(iiay)", []),
            "ToolTip": GLib.Variant(
                "(sa(iiay)ss)",
                (self._icon_name, self._icon_pixmaps, __app_name__, self._tooltip),
            ),
        }
        return mapping.get(name)

    def _emit_sni(self, signal: str) -> None:
        if not self._conn or not self._alive:
            return
        try:
            if signal == "NewStatus":
                self._conn.emit_signal(
                    None,
                    self._item_path,
                    SNI_IFACE,
                    signal,
                    GLib.Variant("(s)", (self._status,)),
                )
            else:
                self._conn.emit_signal(None, self._item_path, SNI_IFACE, signal, None)
        except Exception as e:  # noqa: BLE001
            log.debug("emit %s: %s", signal, e)

    # ── dbusmenu ───────────────────────────────────────────────────────

    def _menu_info(self) -> Gio.DBusNodeInfo:
        return Gio.DBusNodeInfo.new_for_xml(
            f"""
            <node>
              <interface name="{MENU_IFACE}">
                <property name="Version" type="u" access="read"/>
                <property name="TextDirection" type="s" access="read"/>
                <property name="Status" type="s" access="read"/>
                <property name="IconThemePath" type="as" access="read"/>
                <method name="GetLayout">
                  <arg type="i" name="parentId" direction="in"/>
                  <arg type="i" name="recursionDepth" direction="in"/>
                  <arg type="as" name="propertyNames" direction="in"/>
                  <arg type="u" name="revision" direction="out"/>
                  <arg type="(ia{{sv}}av)" name="layout" direction="out"/>
                </method>
                <method name="GetGroupProperties">
                  <arg type="ai" name="ids" direction="in"/>
                  <arg type="as" name="propertyNames" direction="in"/>
                  <arg type="a(ia{{sv}})" name="properties" direction="out"/>
                </method>
                <method name="GetProperty">
                  <arg type="i" name="id" direction="in"/>
                  <arg type="s" name="name" direction="in"/>
                  <arg type="v" name="value" direction="out"/>
                </method>
                <method name="Event">
                  <arg type="i" name="id" direction="in"/>
                  <arg type="s" name="eventId" direction="in"/>
                  <arg type="v" name="data" direction="in"/>
                  <arg type="u" name="timestamp" direction="in"/>
                </method>
                <method name="EventGroup">
                  <arg type="a(isvu)" name="events" direction="in"/>
                  <arg type="ai" name="idErrors" direction="out"/>
                </method>
                <method name="AboutToShow">
                  <arg type="i" name="id" direction="in"/>
                  <arg type="b" name="needUpdate" direction="out"/>
                </method>
                <method name="AboutToShowGroup">
                  <arg type="ai" name="ids" direction="in"/>
                  <arg type="ai" name="updatesNeeded" direction="out"/>
                  <arg type="ai" name="idErrors" direction="out"/>
                </method>
                <signal name="ItemsPropertiesUpdated">
                  <arg type="a(ia{{sv}})" name="updatedProps"/>
                  <arg type="a(ias)" name="removedProps"/>
                </signal>
                <signal name="LayoutUpdated">
                  <arg type="u" name="revision"/>
                  <arg type="i" name="parent"/>
                </signal>
                <signal name="ItemActivationRequested">
                  <arg type="i" name="id"/>
                  <arg type="u" name="timestamp"/>
                </signal>
              </interface>
            </node>
            """
        )

    def _layout_variant(self) -> GLib.Variant:
        show = _menu_node(1, {"type": "standard", "label": "Show SysCare", "enabled": True}, [])
        sep1 = _menu_node(2, {"type": "separator"}, [])
        stats = _menu_node(3, {"type": "standard", "label": self._label_cpu, "enabled": False}, [])
        sep2 = _menu_node(4, {"type": "separator"}, [])
        quit_ = _menu_node(5, {"type": "standard", "label": "Quit", "enabled": True}, [])
        return _menu_node(
            0,
            {"children-display": "submenu"},
            [show, sep1, stats, sep2, quit_],
        )

    def _menu_get_prop(self, _conn, _sender, _path, _iface, name: str):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(name)

    def _menu_method(
        self,
        _conn,
        _sender,
        _path,
        _iface,
        method: str,
        params: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        if method == "GetLayout":
            try:
                out = GLib.Variant.new_tuple(
                    GLib.Variant.new_uint32(self._revision),
                    self._layout_variant(),
                )
                invocation.return_value(out)
            except Exception as e:  # noqa: BLE001
                log.warning("GetLayout: %s", e)
                invocation.return_error_literal(
                    Gio.dbus_error_quark(), Gio.DBusError.FAILED, str(e)
                )
            return
        if method == "GetGroupProperties":
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([],)))
            return
        if method == "GetProperty":
            invocation.return_value(GLib.Variant("(v)", (GLib.Variant("s", ""),)))
            return
        if method == "Event":
            item_id, event_id = params.unpack()[0], params.unpack()[1]
            self._handle_click(item_id, event_id)
            invocation.return_value(None)
            return
        if method == "EventGroup":
            for ev in params.unpack()[0]:
                self._handle_click(ev[0], ev[1])
            invocation.return_value(GLib.Variant("(ai)", ([],)))
            return
        if method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (True,)))
            return
        if method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
            return
        invocation.return_error_literal(
            Gio.dbus_error_quark(),
            Gio.DBusError.UNKNOWN_METHOD,
            f"Unknown method {method}",
        )

    def _handle_click(self, item_id: int, event_id: str) -> None:
        if event_id != "clicked":
            return
        if item_id == 1:
            GLib.idle_add(self._on_show)
        elif item_id == 5:
            GLib.idle_add(self._on_quit)

    def _emit_menu_layout(self) -> None:
        if not self._conn or not self._alive:
            return
        try:
            self._conn.emit_signal(
                None,
                self._menu_path,
                MENU_IFACE,
                "LayoutUpdated",
                GLib.Variant("(ui)", (self._revision, 0)),
            )
        except Exception as e:  # noqa: BLE001
            log.debug("LayoutUpdated: %s", e)
