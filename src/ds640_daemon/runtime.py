from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .config import Config


DRIVER_LIBRARIES = (
    "libsane-brother5.so.1.0.7",
    "libLxBsDeviceAccs.so.1.0.0",
    "libLxBsNetDevAccs.so.1.0.0",
    "libLxBsScanCoreApi.so.3.2.1",
    "libLxBsUsbDevAccs.so.1.0.0",
)


@dataclass(frozen=True)
class UsbDevice:
    sys_path: Path
    bus: int
    device: int
    vendor_id: str
    product_id: str
    manufacturer: str
    product: str
    serial: str

    @property
    def node(self) -> Path:
        return Path(f"/dev/bus/usb/{self.bus:03d}/{self.device:03d}")

    @property
    def description(self) -> str:
        name = " ".join(part for part in (self.manufacturer, self.product) if part)
        return f"{name or 'USB device'} ({self.vendor_id}:{self.product_id}) at {self.node}"


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    sane_config: Path
    sane_libraries: Path
    brother_config: Path
    jobs: Path


def discover_usb(
    config: Config,
    sys_root: Path = Path("/sys/bus/usb/devices"),
) -> list[UsbDevice]:
    wanted_vendor = config.scanner.vendor_id.lower().removeprefix("0x")
    wanted_product = config.scanner.product_id.lower().removeprefix("0x")
    devices: list[UsbDevice] = []
    for sys_path in sorted(sys_root.glob("*")):
        vendor = _read(sys_path / "idVendor").lower()
        product = _read(sys_path / "idProduct").lower()
        if vendor != wanted_vendor or product != wanted_product:
            continue
        try:
            bus = int(_read(sys_path / "busnum"))
            device = int(_read(sys_path / "devnum"))
        except ValueError:
            continue
        devices.append(
            UsbDevice(
                sys_path=sys_path,
                bus=bus,
                device=device,
                vendor_id=vendor,
                product_id=product,
                manufacturer=_read(sys_path / "manufacturer"),
                product=_read(sys_path / "product"),
                serial=_read(sys_path / "serial"),
            )
        )
    return devices


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return ""


def validate_host_dependencies() -> list[str]:
    required = ("bwrap", "scanimage")
    return [name for name in required if shutil.which(name) is None]


def ensure_runtime(config: Config) -> RuntimePaths:
    driver = config.runtime.driver_path
    if not driver.is_dir():
        raise FileNotFoundError(f"Brother driver directory not found: {driver}")

    root = config.runtime.state_path / "runtime"
    sane_config = root / "sane.d"
    sane_libraries = root / "lib"
    brother_config = root / "etc-opt-brother"
    jobs = config.runtime.state_path / "jobs"
    for directory in (sane_config, sane_libraries, brother_config / "scanner", jobs):
        directory.mkdir(parents=True, exist_ok=True)
    (sane_config / "dll.conf").write_text("brother5\n", encoding="utf-8")
    _symlink(driver, brother_config / "scanner" / "brscan5")

    for library in DRIVER_LIBRARIES:
        target = driver / library
        if not target.is_file():
            raise FileNotFoundError(f"Brother driver library not found: {target}")
        names = {library}
        shortened = library
        for _ in range(2):
            head, separator, suffix = shortened.rpartition(".")
            if separator and suffix.isdigit():
                shortened = head
                names.add(shortened)
        for name in names:
            _symlink(target, sane_libraries / name)

    return RuntimePaths(root, sane_config, sane_libraries, brother_config, jobs)


def _symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)


def build_runtime_command(
    config: Config,
    command: list[str],
    *,
    preload: Path | None = None,
    environment: dict[str, str] | None = None,
) -> list[str]:
    paths = ensure_runtime(config)
    driver = config.runtime.driver_path
    brother_dir = driver.parent.parent
    runtime_environment = {
        "SANE_CONFIG_DIR": str(paths.sane_config),
        "LD_LIBRARY_PATH": f"{paths.sane_libraries}:{driver}",
    }
    if environment:
        runtime_environment.update(environment)
    result = ["bwrap", "--die-with-parent", "--unsetenv", "LD_PRELOAD"]
    if preload is not None:
        result.extend(["--setenv", "LD_PRELOAD", str(preload)])
    result.append("--tmpfs")
    result.append("/")
    for path in ("/usr", "/lib", "/lib64", "/bin", "/sbin"):
        if Path(path).exists():
            result.extend(["--ro-bind", path, path])
    result.extend(["--ro-bind", "/home", "/home"])
    result.extend(["--bind", str(config.runtime.state_path), str(config.runtime.state_path)])
    result.extend(["--ro-bind", "/sys", "/sys", "--dev-bind", "/dev", "/dev"])
    result.extend(["--proc", "/proc", "--dir", "/etc"])
    for path in ("/etc/passwd", "/etc/group", "/etc/nsswitch.conf", "/etc/hosts"):
        if Path(path).exists():
            result.extend(["--ro-bind", path, path])
    result.extend(
        [
            "--dir",
            "/etc/opt",
            "--dir",
            "/opt",
            "--bind",
            str(brother_dir),
            "/opt/brother",
            "--bind",
            str(paths.brother_config),
            "/etc/opt/brother",
        ]
    )
    for key, value in runtime_environment.items():
        result.extend(["--setenv", key, value])
    result.extend(command)
    return result


def run_runtime(
    config: Config,
    command: list[str],
    *,
    capture_output: bool = False,
    timeout: float | None = None,
    preload: Path | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        build_runtime_command(config, command, preload=preload, environment=environment),
        check=False,
        capture_output=capture_output,
        text=True,
        timeout=timeout,
    )


def popen_runtime(
    config: Config,
    command: list[str],
    *,
    stdout: int | IO[str] | None = None,
    stderr: int | IO[str] | None = None,
    preload: Path | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        build_runtime_command(config, command, preload=preload, environment=environment),
        text=True,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
