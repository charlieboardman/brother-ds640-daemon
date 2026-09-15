from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from threading import Event, Timer

from . import __version__
from .config import DEFAULT_CONFIG_PATH, Config, expand_path, render_config
from .runtime import discover_usb, run_runtime, validate_host_dependencies
from .scanner import AlreadyRunningError, ScanDaemon


SERVICE_NAME = "brother-ds640.service"


def command_init_config(args: argparse.Namespace) -> int:
    path = expand_path(args.config)
    if path.exists() and not args.force:
        print(f"Configuration already exists: {path}")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(render_config(args.output_dir), encoding="utf-8")
    os.replace(temporary, path)
    path.chmod(0o600)
    print(f"config={path}")
    return 0


def command_discover(args: argparse.Namespace) -> int:
    config = _load_or_default(args.config)
    devices = discover_usb(config)
    if not devices:
        print(
            f"No USB scanner matching {config.scanner.vendor_id}:{config.scanner.product_id} was found."
        )
        return 1
    for index, device in enumerate(devices, 1):
        print(f"scanner[{index}]={device.description}")
        print(f"scanner[{index}].serial={device.serial or '(not reported)'}")
        print(f"scanner[{index}].read_write={'yes' if os.access(device.node, os.R_OK | os.W_OK) else 'no'}")
    if len(devices) > 1:
        print("Multiple identical scanners are not yet supported by the Brother backend wrapper.")
        return 2
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    failures = 0
    try:
        config = Config.load(args.config)
        print(f"config={expand_path(args.config)} status=ok")
    except Exception as exc:
        print(f"config={expand_path(args.config)} status=error error={exc}")
        return 2

    missing = validate_host_dependencies()
    print(f"dependencies={'ok' if not missing else 'missing:' + ','.join(missing)}")
    failures += bool(missing)
    print(f"driver={config.runtime.driver_path} exists={config.runtime.driver_path.is_dir()}")
    failures += not config.runtime.driver_path.is_dir()
    devices = discover_usb(config)
    print(f"usb_matches={len(devices)}")
    for device in devices:
        writable = os.access(device.node, os.R_OK | os.W_OK)
        print(f"usb={device.description} read_write={'yes' if writable else 'no'}")
        failures += not writable
    if len(devices) > 1:
        failures += 1

    if not missing and config.runtime.driver_path.is_dir():
        try:
            probe = run_runtime(
                config,
                ["scanimage", "-L"],
                capture_output=True,
                timeout=15,
            )
            output = f"{probe.stdout}{probe.stderr}".strip()
            recognized = probe.returncode == 0 and "Brother DS-640" in output
            print(f"brother_backend={'ok' if recognized else 'failed'}")
            if output:
                print(output)
            failures += not recognized
        except Exception as exc:
            print(f"brother_backend=error error={exc}")
            failures += 1
    print(f"status={'ready' if failures == 0 else 'not_ready'}")
    return 0 if failures == 0 else 1


def command_run(args: argparse.Namespace) -> int:
    try:
        config = Config.load(args.config)
        daemon = ScanDaemon(config)
    except Exception as exc:
        print(f"startup_error={exc}", file=sys.stderr)
        return 1

    stop_event = Event()

    def stop(_signum=None, _frame=None) -> None:
        stop_event.set()

    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    for number in previous:
        signal.signal(number, stop)
    try:
        daemon.run(stop_event, max_pages=args.max_pages)
        return 0
    except AlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def command_test_scan(args: argparse.Namespace) -> int:
    try:
        config = Config.load(args.config)
        before = set(config.output.path.glob("*.pdf")) if config.output.path.exists() else set()
    except Exception as exc:
        print(f"startup_error={exc}", file=sys.stderr)
        return 1
    stop_event = Event()
    timer = Timer(args.timeout, stop_event.set) if args.timeout > 0 else None
    if timer:
        timer.start()
    print(f"Feed one sheet into the DS-640. Waiting up to {args.timeout:g} seconds...")
    try:
        pages = ScanDaemon(config).run(stop_event, max_pages=1)
    except AlreadyRunningError:
        print(f"Stop the service first: systemctl --user stop {SERVICE_NAME}", file=sys.stderr)
        return 2
    finally:
        if timer:
            timer.cancel()
    after = set(config.output.path.glob("*.pdf"))
    created = sorted(after - before, key=lambda path: path.stat().st_mtime_ns)
    if pages and created:
        print(f"test_scan={created[-1]}")
        return 0
    print("No test page was received before the timeout.", file=sys.stderr)
    return 1


def command_service_install(args: argparse.Namespace) -> int:
    if os.geteuid() == 0:
        print("Install the user service as the account that will receive scans, not as root.")
        return 2
    config_path = expand_path(args.config)
    Config.load(config_path)
    executable = Path(sys.argv[0]).resolve()
    destination = _service_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    previous = destination.read_bytes() if destination.exists() else None
    temporary = destination.with_suffix(".service.tmp")
    temporary.write_text(_render_service(executable, config_path), encoding="utf-8")
    os.replace(temporary, destination)
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", SERVICE_NAME], check=True)
        if not args.no_start:
            subprocess.run(["systemctl", "--user", "restart", SERVICE_NAME], check=True)
            active = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", SERVICE_NAME],
                check=False,
            )
            if active.returncode:
                raise RuntimeError("the service did not become active")
    except Exception:
        if previous is None:
            destination.unlink(missing_ok=True)
        else:
            restore = destination.with_suffix(".service.restore")
            restore.write_bytes(previous)
            os.replace(restore, destination)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        raise
    print(f"service={destination}")
    print(f"started={'no' if args.no_start else 'yes'}")
    return 0


def command_service_status(_args: argparse.Namespace) -> int:
    return subprocess.run(
        ["systemctl", "--user", "status", SERVICE_NAME, "--no-pager"],
        check=False,
    ).returncode


def command_service_uninstall(_args: argparse.Namespace) -> int:
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", SERVICE_NAME],
        check=False,
    )
    _service_path().unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "reset-failed"], check=False)
    print(f"removed={_service_path()}")
    return 0


def _load_or_default(path: str | Path) -> Config:
    config_path = expand_path(path)
    return Config.load(config_path) if config_path.exists() else Config()


def _service_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _render_service(executable: Path, config: Path) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Brother DS-640 always-ready scan ingestion daemon",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={_systemd_quote(str(executable))} --config {_systemd_quote(str(config))} run",
            "Restart=on-failure",
            "RestartSec=3",
            "TimeoutStopSec=20",
            "KillMode=control-group",
            "Environment=PYTHONUNBUFFERED=1",
            "UMask=0007",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Always-ready Brother DS-640 scanner daemon")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Configuration file (default: {DEFAULT_CONFIG_PATH})",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init-config", help="Write a default configuration")
    init.add_argument("--output-dir", default="~/Scans/Inbox")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=command_init_config)

    discover = commands.add_parser("discover", help="Find matching scanners on any USB port")
    discover.set_defaults(func=command_discover)
    doctor = commands.add_parser("doctor", help="Check dependencies, USB access, and Brother backend")
    doctor.set_defaults(func=command_doctor)

    run = commands.add_parser("run", help="Run the ingestion daemon in the foreground")
    run.add_argument("--max-pages", type=int, default=0, help="Stop after N pages; zero means forever")
    run.set_defaults(func=command_run)

    test = commands.add_parser("test-scan", help="Wait for and validate one test sheet")
    test.add_argument("--timeout", type=float, default=120.0)
    test.set_defaults(func=command_test_scan)

    install = commands.add_parser("service-install", help="Install and enable the systemd user service")
    install.add_argument("--no-start", action="store_true")
    install.set_defaults(func=command_service_install)
    status = commands.add_parser("service-status", help="Show the systemd user service status")
    status.set_defaults(func=command_service_status)
    uninstall = commands.add_parser("service-uninstall", help="Remove the systemd user service")
    uninstall.set_defaults(func=command_service_uninstall)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
