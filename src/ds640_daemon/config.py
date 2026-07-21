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
    # Retained for compatibility with the short-lived 1.1.0 config. Brother's
    # auto-size mode stalls image transfer when combined with ATCN on a DS-640,
    # so the daemon deliberately ignores this value.
    auto_document_size: bool = False
    image_format: str = "png"
    extra_args: tuple[str, ...] = ()
    poll_seconds: float = 0.25
    empty_retry_seconds: float = 2.0
    session_restart_seconds: float = 1.0
    part_settle_seconds: float = 5.0
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
        if self.scanner.atcn_timeout <= 0 or self.scanner.atcn_feed_delay < 0:
            raise ValueError("scanner.atcn_timeout must be positive and atcn_feed_delay non-negative")
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
image_format = "png"
extra_args = []
poll_seconds = 0.25
empty_retry_seconds = 2.0
session_restart_seconds = 1.0
part_settle_seconds = 5.0
# Undocumented Brother ATCN wait parameters.
atcn_timeout = 7200
atcn_feed_delay = 30

[output]
directory = "{output}"
filename_prefix = "scan"
keep_images = false

[runtime]
driver_directory = "{runtime.driver_directory}"
tap_library = "{runtime.tap_library}"
state_directory = "{runtime.state_directory}"
quiet = true
cleanup_days = 7
'''
