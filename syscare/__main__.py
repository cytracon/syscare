"""CLI entry: python3 -m syscare"""

from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="syscare", description="SysCare — system optimizer & monitor")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    parser.add_argument(
        "--tray",
        "--start-hidden",
        dest="start_hidden",
        action="store_true",
        help="Start hidden in the system tray",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="Disable tray icon (window close quits the app)",
    )
    parser.add_argument(
        "--autostart",
        choices=("on", "off", "status"),
        help="Enable/disable start on login (tray), or show status",
    )
    parser.add_argument(
        "--clean-safe",
        action="store_true",
        help="Clean safe user-cache targets without opening the GUI",
    )
    parser.add_argument(
        "--security-audit",
        action="store_true",
        help="Print the Linux security posture audit as JSON",
    )
    parser.add_argument(
        "--check-updates",
        action="store_true",
        help="Print available Pacman/Omarchy/AUR/Flatpak updates as JSON",
    )
    parser.add_argument(
        "--schedule",
        choices=("daily", "weekly", "monthly", "off", "status"),
        help="Configure safe scheduled maintenance",
    )
    parser.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print a compact JSON snapshot for the Omarchy bar plugin",
    )
    args, rest = parser.parse_known_args(argv)

    if args.version:
        from . import __version__

        print(__version__)
        return 0

    if args.autostart is not None:
        from . import autostart as asmod

        if args.autostart == "status":
            print(asmod.status_text())
            return 0
        if args.autostart == "on":
            ok, msg = asmod.enable()
            print(("enabled: " if ok else "failed: ") + msg)
            return 0 if ok else 1
        ok, msg = asmod.disable()
        print(("disabled: " if ok else "failed: ") + msg)
        return 0 if ok else 1

    if args.schedule is not None:
        from . import scheduler

        if args.schedule == "status":
            status = scheduler.get_status()
            print(json.dumps(status.__dict__, indent=2))
            return 0
        if args.schedule == "off":
            ok, msg = scheduler.disable_schedule()
        else:
            ok, msg = scheduler.set_schedule(args.schedule)
        print(msg)
        return 0 if ok else 1

    if args.clean_safe:
        from . import cleaner, history

        targets = [
            target
            for target in cleaner.scan_targets()
            if target.risk == "safe" and not target.needs_root and target.size > 0
        ]
        for target in targets:
            target.selected = True
        results = cleaner.clean_targets(targets)
        freed = sum(result.freed for result in results)
        failures = [result for result in results if not result.ok]
        summary = f"Cleaned {len(results) - len(failures)}/{len(results)} safe target(s); freed ~{freed} bytes"
        history.add_entry(
            "Scheduled safe clean" if args.scheduled else "CLI safe clean",
            "warning" if failures else "ok",
            summary,
            reclaimed=freed,
            details={"failures": [result.message for result in failures[:20]]},
        )
        print(summary)
        return 1 if failures else 0

    if args.security_audit:
        from dataclasses import asdict

        from .security import audit

        print(json.dumps([asdict(check) for check in audit()], indent=2))
        return 0

    if args.status:
        from . import sysinfo

        print(json.dumps(sysinfo.status_payload(), indent=2))
        return 0

    if args.check_updates:
        from dataclasses import asdict

        from .updater import list_updates

        items, errors = list_updates()
        print(json.dumps({"updates": [asdict(item) for item in items], "errors": errors}, indent=2))
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    gtk_argv = [sys.argv[0], *rest]
    from .app import run_app

    return run_app(
        gtk_argv,
        start_hidden=bool(args.start_hidden),
        no_tray=bool(args.no_tray),
    )


if __name__ == "__main__":
    raise SystemExit(main())
