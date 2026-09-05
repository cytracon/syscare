#!/usr/bin/env bash
# Install SysCare for the current user on Omarchy (Arch). No root required
# unless optional packages are missing.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_BASE="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
LIB_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/syscare"
APP_ID="com.bbachmann.syscare"
PKG_NAME="syscare"

echo "==> SysCare ${PKG_NAME} (Omarchy / Arch, user-local)"
mkdir -p "$BIN_DIR" "$APP_DIR" "$LIB_DIR" "$LIB_DIR/icons"

rsync -a --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "$ROOT/syscare/" "$LIB_DIR/syscare/"

cat > "$BIN_DIR/${PKG_NAME}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
LIB_LOCAL="$LIB_DIR"
export PYTHONPATH="\${LIB_LOCAL}\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m syscare "\$@"
EOF
chmod 0755 "$BIN_DIR/${PKG_NAME}"
chmod 0755 "$ROOT/bin/${PKG_NAME}" 2>/dev/null || true

SVG_SRC="$ROOT/data/icons/syscare.svg"
install -D -m 644 "$SVG_SRC" "$ICON_BASE/scalable/apps/${APP_ID}.svg"
install -D -m 644 "$SVG_SRC" "$ICON_BASE/scalable/apps/${PKG_NAME}.svg"
cp -f "$SVG_SRC" "$LIB_DIR/icons/${APP_ID}.svg"

PNG_SRC="$ROOT/data/icons/png"
if [[ -d "$PNG_SRC" ]]; then
  for size in 16 22 24 32 48 64 128 256 512; do
    src="$PNG_SRC/${size}x${size}/${APP_ID}.png"
    if [[ -f "$src" ]]; then
      install -D -m 644 "$src" "$ICON_BASE/${size}x${size}/apps/${APP_ID}.png"
      cp -f "$src" "$ICON_BASE/${size}x${size}/apps/${PKG_NAME}.png"
    fi
  done
fi

rm -f "$APP_DIR/${PKG_NAME}.desktop"
install -m 644 "$ROOT/data/${APP_ID}.desktop" "$APP_DIR/${APP_ID}.desktop"
desk="$APP_DIR/${APP_ID}.desktop"
sed -i "s|^Exec=.*|Exec=$BIN_DIR/${PKG_NAME}|" "$desk"
sed -i "s|^Icon=.*|Icon=${APP_ID}|" "$desk"
if grep -q '^StartupWMClass=' "$desk"; then
  sed -i "s|^StartupWMClass=.*|StartupWMClass=${APP_ID}|" "$desk"
else
  echo "StartupWMClass=${APP_ID}" >> "$desk"
fi

need=()
python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" 2>/dev/null \
  || need+=(python-gobject gtk4 libadwaita)
python3 -c "import psutil" 2>/dev/null || need+=(python-psutil)
command -v checkupdates >/dev/null 2>&1 || need+=(pacman-contrib)
command -v pkexec >/dev/null 2>&1 || need+=(polkit)

if ((${#need[@]})); then
  echo "==> Missing packages: ${need[*]}"
  if command -v omarchy >/dev/null 2>&1; then
    echo "    Installing with omarchy pkg add (sudo may be requested)…"
    omarchy pkg add "${need[@]}"
  else
    echo "    Install with: sudo pacman -S --needed ${need[*]}"
  fi
fi

if command -v update-desktop-database >/dev/null; then
  update-desktop-database "$APP_DIR" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null; then
  gtk-update-icon-cache -f -t "$ICON_BASE" 2>/dev/null || true
fi

echo
echo "Installed:"
echo "  binary : $BIN_DIR/${PKG_NAME}"
echo "  package: $LIB_DIR"
echo "  desktop: $APP_DIR/${APP_ID}.desktop"
echo "  icon   : $ICON_BASE/scalable/apps/${APP_ID}.svg"
echo
echo "Start with:  ${PKG_NAME}"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo "  PATH tip: export PATH=\"$BIN_DIR:\$PATH\""
fi
