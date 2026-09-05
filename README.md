# SysCare

**What it is.** SysCare is a system optimizer for **Omarchy** (Arch Linux + Hyprland). Use it like Stacer: clean caches, check pacman/Omarchy/AUR/Flatpak updates, inspect processes and services, review security (firewall, lock screen, SSH), and uninstall packages. Privileged steps go through pkexec.

GTK4 + libadwaita, Python. Replaces the old Ubuntu/Stacer build.

The Omarchy bar plugin is a separate repository: [cytracon/omarchy-syscare](https://github.com/cytracon/omarchy-syscare).

## Install on Omarchy

User-local, no root unless packages are missing:

```bash
git clone https://github.com/cytracon/syscare.git
cd syscare
./install-local.sh
```

Or, after the user-local Omarchy CLI overlay is in `~/.local/bin`:

```bash
omarchy install syscare
```

Dependencies (Arch packages, not pip):

- `python` `python-gobject` `python-psutil` `gtk4` `libadwaita`
- `pacman-contrib` (for `checkupdates`)
- `polkit` (for `pkexec` on privileged actions)

Missing packages are installed with `omarchy pkg add`.

Remove the app:

```bash
rm -f ~/.local/bin/syscare ~/.local/bin/omarchy-install-syscare
rm -rf ~/.local/share/syscare
rm -f ~/.local/share/applications/com.bbachmann.syscare.desktop
rm -f ~/.config/autostart/com.bbachmann.syscare-tray.desktop
```

Removing the app does not remove the bar plugin.

## Features

| Page | Function |
|------|----------|
| **Dashboard** | Live gauges, care score, quick actions |
| **System Cleaner** | User caches, pacman cache, journal, trash, orphans |
| **Security & Malware** | Firewall, LUKS, Secure Boot, Omarchy screen lock, SSH, Fail2ban, optional ClamAV |
| **Software Updates** | Pacman, Omarchy, AUR (yay/paru), Flatpak |
| **Storage / Network / Processes / Services** | Same as the previous Linux build |
| **Uninstaller** | `pacman -R` / `-Rns` with a print preview |
| **Schedules** | systemd user timer for `--clean-safe` |

Root actions go through **pkexec** (Omarchy polkit). The cleaner batches root steps into one password prompt.

## CLI

```bash
syscare --status              # compact JSON for the bar plugin
syscare --clean-safe          # safe user caches only; no root targets
syscare --security-audit      # JSON
syscare --check-updates       # Pacman / Omarchy / AUR / Flatpak as JSON
syscare --schedule weekly     # daily | weekly | monthly | off | status
syscare --autostart on        # login → tray hidden
```

## Plugin

```bash
omarchy plugin add https://github.com/cytracon/omarchy-syscare.git --enable
```

The plugin does not ship this GTK app. Left-click opens a status panel; the panel launches `~/.local/bin/syscare`.

## License

MIT. Cleaner catalogue under `syscare/cleaner_rules/` is adapted from Kudu (MIT); see `THIRD-PARTY-NOTICES.md`.
