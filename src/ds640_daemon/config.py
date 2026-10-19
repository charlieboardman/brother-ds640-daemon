from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


APP_NAME = "brother-ds640"


def _environment_path(name: str, fallback: str) -> Path:
    return Path(os.environ.get(name) or fallback).expanduser()


DEFAULT_CONFIG_PATH = (
    _environment_path("XDG_CONFIG_HOME", "~/.config")
    / APP_NAME
    / "config.toml"
)


def expand_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser().resolve()


@dataclass(frozen=True)
class ScannerConfig:
    vendor_id: str = "04f9"
    product_id: str = "0468"
    resolution: int = 300
    mode: str = ""
    source: str = ""
    auto_document_size: bool = True
    scan_timeout_seconds: float = 180.0
    image_format: str = "png"
    extra_args: tuple[str, ...] = ()
    poll_seconds: float = 0.25
    empty_retry_seconds: float = 2.0
    session_restart_seconds: float = 1.0
    part_settle_seconds: float = 5.0
    # The brscan5 SANE frontend caps the normal scan height at 355.6 mm
    # (14 in).  The DS-640 firmware supports up to 1828.8 mm (72 in) in
    # simplex long-paper mode; the bundled shim exposes that path.
    long_paper: bool = False
    long_paper_paper_type: int = 24  # 23 = narrow, 24 = full width
    long_paper_width_mm: float = 215.9
    max_document_length_mm: float = 1828.8
    # Accepted for compatibility with old config files; unused.
    atcn_timeout: int = 7200
    atcn_feed_delay: int = 30


@dataclass(frozen=True)
class OutputConfig:
    directory: str = "~/Scans/Inbox"
    filename_prefix: str = "scan"
    keep_images: bool = False

    @property
    def path(self) -> Path:
        return expand_path(self.directory)


@dataclass(frozen=True)
class RuntimeConfig:
    driver_directory: str = str(
        _environment_path("XDG_DATA_HOME", "~/.local/share")
        / APP_NAME
        / "driver/opt/brother/scanner/brscan5"
    )
    tap_library: str = str(Path("~/.local/lib/brother-ds640/libds640_atcn.so").expanduser())
    long_paper_library: str = str(
        Path("~/.local/lib/brother-ds640/libds640_longpaper.so").expanduser()
    )
    state_directory: str = str(
        _environment_path("XDG_STATE_HOME", "~/.local/state") / APP_NAME
    )
    quiet: bool = True
    cleanup_days: int = 7

    @property
    def driver_path(self) -> Path:
        return expand_path(self.driver_directory)

    @property
    def tap_path(self) -> Path:
        return expand_path(self.tap_library)

    @property
    def long_paper_path(self) -> Path:
        return expand_path(self.long_paper_library)

    @property
    def state_path(self) -> Path:
        return expand_path(self.state_directory)


@dataclass(frozen=True)
class Config:
    scanner: ScannerConfig = ScannerConfig()
    output: OutputConfig = OutputConfig()
    runtime: RuntimeConfig = RuntimeConfig()

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> "Config":
        config_path = expand_path(path)
        if not config_path.exists():
            raise FileNotFoundError(
                f"Configuration not found: {config_path}. Run `ds640 init-config`."
            )
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
        config = cls(
            scanner=_section(ScannerConfig, data.get("scanner", {})),
            output=_section(OutputConfig, data.get("output", {})),
            runtime=_section(RuntimeConfig, data.get("runtime", {})),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.scanner.image_format not in {"png", "jpeg", "tiff"}:
            raise ValueError("scanner.image_format must be png, jpeg, or tiff")
        if self.scanner.resolution not in {100, 150, 200, 300, 400, 600, 1200}:
            raise ValueError("scanner.resolution is not supported by the DS-640")
        for name in ("poll_seconds", "empty_retry_seconds", "part_settle_seconds"):
            if getattr(self.scanner, name) < 0:
                raise ValueError(f"scanner.{name} must not be negative")
        if self.scanner.scan_timeout_seconds <= 0:
            raise ValueError("scanner.scan_timeout_seconds must be positive")
        if self.scanner.auto_document_size and self.scanner.resolution > 400:
            raise ValueError("Automatic sizing requires resolution <= 400 DPI on the DS-640")
        if self.scanner.long_paper_paper_type not in {23, 24}:
            raise ValueError("scanner.long_paper_paper_type must be 23 (narrow) or 24 (full width)")
        max_width_mm = 107.95 if self.scanner.long_paper_paper_type == 23 else 215.9
        if not 0 < self.scanner.long_paper_width_mm <= max_width_mm:
            raise ValueError(
                "scanner.long_paper_width_mm must be between 0 and "
                f"{max_width_mm} for paper type {self.scanner.long_paper_paper_type}"
            )
        if not 0 < self.scanner.max_document_length_mm <= 1828.8:
            raise ValueError("scanner.max_document_length_mm must be between 0 and 1828.8")
        if self.scanner.long_paper and self.scanner.resolution > 300:
            raise ValueError("Long-paper scanning requires resolution <= 300 DPI on the DS-640")
        if self.runtime.cleanup_days < 1:
            raise ValueError("runtime.cleanup_days must be at least 1")
        if not self.output.filename_prefix or "/" in self.output.filename_prefix:
            raise ValueError("output.filename_prefix must be a non-empty filename prefix")


def _section(section_type, values: Any):
    if not isinstance(values, dict):
        raise ValueError(f"[{section_type.__name__.removesuffix('Config').lower()}] must be a table")
    allowed = {field.name for field in fields(section_type)}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    normalized = dict(values)
    if section_type is ScannerConfig and "extra_args" in normalized:
        normalized["extra_args"] = tuple(normalized["extra_args"])
    return section_type(**normalized)


def render_config(output_dir: str | Path = "~/Scans/Inbox") -> str:
    output = str(output_dir).replace("\\", "\\\\").replace('"', '\\"')
    runtime = RuntimeConfig()
    return f'''# Brother DS-640 daemon configuration

[scanner]
# USB identity; this follows the scanner across ports and reconnects.
vendor_id = "04f9"
product_id = "0468"
resolution = 300
# Empty strings let Brother's backend choose its device defaults.
mode = ""
source = ""
auto_document_size = true
scan_timeout_seconds = 180.0
image_format = "png"
extra_args = []
poll_seconds = 0.25
empty_retry_seconds = 2.0
session_restart_seconds = 1.0
part_settle_seconds = 5.0
# Let the DS-640 scan receipts and other documents up to 72 inches long.
# Requires the bundled long-paper shim (installed by install.sh).
long_paper = false
# 23 = long paper narrow (107.95 mm), 24 = long paper full width (215.9 mm).
long_paper_paper_type = 24
long_paper_width_mm = 215.9
max_document_length_mm = 1828.8

[output]
directory = "{output}"
filename_prefix = "scan"
keep_images = false

[runtime]
driver_directory = "{runtime.driver_directory}"
long_paper_library = "{runtime.long_paper_library}"
state_directory = "{runtime.state_directory}"
quiet = true
cleanup_days = 7
'''
