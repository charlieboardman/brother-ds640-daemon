from __future__ import annotations

import datetime as dt
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

import img2pdf
from PIL import Image, ImageChops, ImageFile

from .config import Config
from .runtime import discover_usb, ensure_runtime, popen_runtime


class AlreadyRunningError(RuntimeError):
    pass


class ScanDaemon:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.paths = ensure_runtime(config)
        self._last_status: str | None = None
        if config.scanner.long_paper and not config.runtime.long_paper_path.is_file():
            raise FileNotFoundError(
                f"Long-paper shim not found: {config.runtime.long_paper_path}. "
                "Reinstall the daemon or set scanner.long_paper = false."
            )

    def run(self, stop_event: Event, *, max_pages: int = 0) -> int:
        self.config.output.path.mkdir(parents=True, exist_ok=True)
        lock_handle = self._lock()
        del lock_handle  # The descriptor remains open for this process lifetime.
        self.cleanup_temporary_files()
        total_pages = self.recover_jobs()
        require_empty = False
        self.status("ready", output=self.config.output.path)

        while not stop_event.is_set():
            # A crash may leave a very recent .part file that was not old
            # enough during the first recovery pass. Recheck between sessions.
            total_pages += self.recover_jobs()
            if max_pages and total_pages >= max_pages:
                return total_pages

            devices = discover_usb(self.config)
            if not devices:
                self.status("waiting_for_scanner")
                stop_event.wait(self.config.scanner.empty_retry_seconds)
                continue
            if len(devices) > 1:
                self.status("multiple_scanners", count=len(devices))
                stop_event.wait(10)
                continue
            if not os.access(devices[0].node, os.R_OK | os.W_OK):
                self.status("usb_permission_denied", node=devices[0].node)
                stop_event.wait(10)
                continue

            self.status("waiting_for_removal" if require_empty else "waiting_for_sheet")
            presence = self._wait_for_paper(stop_event, require_empty=require_empty)
            if stop_event.is_set():
                break
            if presence != 0:
                if presence != 2:
                    self.status("paper_sensor_error", exit=presence)
                stop_event.wait(self.config.scanner.empty_retry_seconds)
                continue
            if require_empty:
                require_empty = False
                continue

            remaining = max_pages - total_pages if max_pages else 0
            pages, exit_code = self._run_session(stop_event, max_pages=remaining)
            total_pages += pages
            require_empty = exit_code != 0 or pages == 0
            if stop_event.is_set():
                break
            if pages:
                self.status("rearming", pages=pages, exit=exit_code)
                stop_event.wait(self.config.scanner.session_restart_seconds)
            else:
                self.status("waiting_for_sheet")
                stop_event.wait(self.config.scanner.empty_retry_seconds)

        self.status("stopped", pages=total_pages)
        return total_pages

    def _lock(self):
        self.config.runtime.state_path.mkdir(parents=True, exist_ok=True)
        handle = (self.config.runtime.state_path / "daemon.lock").open("w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise AlreadyRunningError("another DS-640 daemon is already running") from exc
        # Keep the Python file object reachable through the daemon.
        self._lock_handle = handle
        return handle

    def _wait_for_paper(self, stop_event: Event, *, require_empty: bool = False) -> int:
        command = [sys.executable, "-m", "ds640_daemon.presence",
                   "--interval", str(max(0.1, self.config.scanner.poll_seconds))]
        if require_empty:
            command.append("--empty")
        process = popen_runtime(self.config, command, stdout=subprocess.DEVNULL)
        return self._wait_process(process, stop_event, timeout=40.0)

    def _wait_process(self, process, stop_event: Event, *, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if stop_event.wait(0.1):
                    return 130
                if time.monotonic() >= deadline:
                    self.event("process_timeout", seconds=timeout)
                    return 124
            return process.returncode
        finally:
            self._terminate(process)

    def _run_session(self, stop_event: Event, *, max_pages: int = 0) -> tuple[int, int]:
        session_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        job = self.paths.jobs / f"session-{session_id}"
        job.mkdir(parents=True, exist_ok=False)
        # Distinguish new, complete-page scans from recoverable legacy ATCN jobs.
        (job / ".normal-scan").touch()
        stdio = subprocess.DEVNULL if self.config.runtime.quiet else None
        self.status("scanning")
        preload = None
        environment = None
        if self.config.scanner.long_paper:
            preload = self.config.runtime.long_paper_path
            environment = {
                "DS640_LONG_PAPER_FORCE": "1",
                "DS640_LONG_PAPER_TYPE": str(self.config.scanner.long_paper_paper_type),
                "DS640_LONG_PAPER_WIDTH_MM": str(self.config.scanner.long_paper_width_mm),
                "DS640_LONG_PAPER_HEIGHT_MM": str(self.config.scanner.max_document_length_mm),
            }
        process = popen_runtime(
            self.config,
            self._scanimage_command(job, 1),
            stdout=stdio,
            stderr=stdio,
            preload=preload,
            environment=environment,
        )
        exit_code = self._wait_process(process, stop_event,
                                      timeout=self.config.scanner.scan_timeout_seconds)
        pages = 0
        for image in self._images(job):
            # scanimage renames .part only when the page is complete. Never
            # publish a truncated new scan, even when it has stopped growing.
            if image.name.endswith(".part") or not self._complete(image):
                continue
            output = self._deliver(image, job, session_id)
            pages += 1
            self.event("page_saved", file=output, page=pages)
        if exit_code or not pages:
            self.event("scan_failed", exit=exit_code, recovery_directory=job)
        self._clean_delivered(job)
        self._remove_empty(job)
        return pages, exit_code

    @staticmethod
    def _complete(image: Path) -> bool:
        try:
            with Image.open(image) as opened:
                opened.verify()
            with Image.open(image) as opened:
                opened.load()
            return True
        except (OSError, ValueError, SyntaxError):
            return False

    def _scanimage_command(self, job: Path, max_pages: int) -> list[str]:
        scanner = self.config.scanner
        template = job / f"page-%04d.{scanner.image_format}"
        command = [
            "scanimage",
            f"--format={scanner.image_format}",
            f"--batch={template}",
            "--progress",
            "--resolution",
            str(scanner.resolution),
        ]
        command.append("--batch-count=1")
        command.append(f"--AutoDocumentSize={'yes' if scanner.auto_document_size else 'no'}")
        if scanner.long_paper:
            command.extend(["-y", str(scanner.max_document_length_mm)])
        if scanner.mode:
            command.extend(["--mode", scanner.mode])
        if scanner.source:
            command.extend(["--source", scanner.source])
        command.extend(scanner.extra_args)
        return command

    def _images(self, job: Path) -> list[Path]:
        extension = self.config.scanner.image_format
        images = list(job.glob(f"*.{extension}")) + list(job.glob(f"*.{extension}.part"))
        return sorted(images, key=lambda path: self._identity(path))

    @staticmethod
    def _identity(image: Path) -> str:
        return image.name[:-5] if image.name.endswith(".part") else image.name

    @staticmethod
    def _recoverable(image: Path) -> bool:
        old_setting = ImageFile.LOAD_TRUNCATED_IMAGES
        try:
            ImageFile.LOAD_TRUNCATED_IMAGES = True
            with Image.open(image) as opened:
                if opened.width <= 0 or opened.height <= 0:
                    return False
                opened.load()
            return True
        except (OSError, ValueError, SyntaxError):
            return False
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = old_setting

    @staticmethod
    def _is_scanner_background(color: tuple[int, ...]) -> bool:
        red, green, blue = color[:3]
        return (
            140 <= red <= 225
            and 140 <= green <= 225
            and 140 <= blue <= 225
            and max(red, green, blue) - min(red, green, blue) <= 30
        )

    @staticmethod
    def _trimmed_image(image: Path, job: Path, identity: str) -> Path | None:
        """Crop the scanner's background from a wide long-paper scan.

        Long-paper mode scans the full 215.9 mm width even when the document
        is narrower.  The feeder is left-aligned, so the right edge is the
        scanner's uniform gray background.  Return a temporary trimmed copy,
        or None when the page already fills the width or the corner is not
        the scanner background.
        """
        try:
            with Image.open(image) as opened:
                opened.load()
                rgb = opened.convert("RGB")
                try:
                    dpi = opened.info.get("dpi", (300.0, 300.0))
                    background = rgb.getpixel((rgb.width - 1, 0))
                    if not ScanDaemon._is_scanner_background(background):
                        return None
                    difference = ImageChops.difference(
                        rgb, Image.new("RGB", rgb.size, background)
                    )
                    try:
                        mask = difference.convert("L").point(
                            lambda value: 255 if value > 20 else 0
                        )
                        bbox = mask.getbbox()
                    finally:
                        difference.close()
                    if bbox is None:
                        return None
                    left, top, right, bottom = bbox
                    padding = 12
                    left = max(0, left - padding)
                    top = max(0, top - padding)
                    right = min(rgb.width, right + padding)
                    bottom = min(rgb.height, bottom + padding)
                    if (left, top, right, bottom) == (0, 0, rgb.width, rgb.height):
                        return None
                    trimmed = rgb.crop((left, top, right, bottom))
                    try:
                        temporary = job / f".{identity}.trimmed-{time.monotonic_ns()}.png"
                        trimmed.save(temporary, format="PNG", dpi=dpi)
                        return temporary
                    finally:
                        trimmed.close()
                finally:
                    rgb.close()
        except (OSError, ValueError):
            return None

    def _deliver(self, image: Path, job: Path, session_id: str) -> Path:
        identity = self._identity(image)
        page_stem = Path(identity).stem
        safe_prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", self.config.output.filename_prefix)
        output = self.config.output.path / f"{safe_prefix}-{session_id}-{page_stem}.pdf"
        marker = job / f".{identity}.delivered"
        if output.is_file():
            marker.write_text(str(output), encoding="utf-8")
            return output

        source = image
        copy: Path | None = None
        truncated = image.name.endswith(".part")
        if truncated:
            copy = job / f".{identity}.snapshot-{time.monotonic_ns()}"
            shutil.copy2(image, copy)
            source = copy
        elif self.config.scanner.long_paper:
            copy = self._trimmed_image(image, job, identity)
            if copy is not None:
                source = copy
        try:
            self._image_to_pdf(source, output, truncated=truncated)
            marker.write_text(str(output), encoding="utf-8")
        finally:
            if copy is not None:
                copy.unlink(missing_ok=True)
        if not truncated and not self.config.output.keep_images:
            image.unlink(missing_ok=True)
        return output

    @staticmethod
    def _image_to_pdf(image: Path, output: Path, *, truncated: bool) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{os.getpid()}-{time.monotonic_ns()}.tmp")
        try:
            if not truncated:
                temporary.write_bytes(img2pdf.convert(str(image)))
            else:
                old_setting = ImageFile.LOAD_TRUNCATED_IMAGES
                try:
                    ImageFile.LOAD_TRUNCATED_IMAGES = True
                    with Image.open(image) as source:
                        source.load()
                        dpi = source.info.get("dpi", (72.0, 72.0))
                        resolution = float(dpi[0]) if dpi and dpi[0] > 0 else 72.0
                        converted = source.copy() if source.mode == "RGB" else source.convert("RGB")
                    try:
                        converted.save(temporary, format="PDF", resolution=resolution)
                    finally:
                        converted.close()
                finally:
                    ImageFile.LOAD_TRUNCATED_IMAGES = old_setting
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)

    def recover_jobs(self) -> int:
        recovered = 0
        if not self.paths.jobs.exists():
            return 0
        for job in sorted(self.paths.jobs.glob("session-*")):
            session_id = job.name.removeprefix("session-")
            normal_scan = (job / ".normal-scan").exists()
            for image in self._images(job):
                identity = self._identity(image)
                marker = job / f".{identity}.delivered"
                if marker.exists():
                    try:
                        delivered_output = Path(marker.read_text(encoding="utf-8").strip())
                    except (OSError, UnicodeError):
                        delivered_output = Path()
                    if delivered_output.is_file():
                        if not self.config.output.keep_images:
                            image.unlink(missing_ok=True)
                            marker.unlink(missing_ok=True)
                        continue
                    if not delivered_output.is_file():
                        marker.unlink(missing_ok=True)
                if normal_scan and (image.name.endswith(".part") or not self._complete(image)):
                    continue
                if image.name.endswith(".part"):
                    try:
                        age = time.time() - image.stat().st_mtime
                    except FileNotFoundError:
                        continue
                    if age < self.config.scanner.part_settle_seconds or not self._recoverable(image):
                        continue
                output = self._deliver(image, job, session_id)
                recovered += 1
                self.event("page_recovered", file=output)
            self._clean_delivered(job)
            self._remove_empty(job)
        return recovered

    def _clean_delivered(self, job: Path) -> None:
        if self.config.output.keep_images:
            return
        for marker in job.glob(".*.delivered"):
            identity = marker.name[1 : -len(".delivered")]
            try:
                output = Path(marker.read_text(encoding="utf-8").strip())
            except (OSError, UnicodeError):
                continue
            if not output.is_file():
                continue
            (job / identity).unlink(missing_ok=True)
            (job / f"{identity}.part").unlink(missing_ok=True)
            marker.unlink(missing_ok=True)

    def cleanup_temporary_files(self) -> None:
        cutoff = time.time() - max(1, self.config.runtime.cleanup_days) * 86400
        output = self.config.output.path
        if output.exists():
            for temporary in output.glob(".*.tmp"):
                try:
                    if temporary.stat().st_mtime < cutoff:
                        temporary.unlink()
                except FileNotFoundError:
                    pass
        for job in self.paths.jobs.glob("session-*"):
            self._remove_empty(job)

    @staticmethod
    def _remove_empty(job: Path) -> None:
        try:
            if set(job.iterdir()) == {job / ".normal-scan"}:
                (job / ".normal-scan").unlink()
            job.rmdir()
        except OSError:
            pass

    @staticmethod
    def _terminate(process: subprocess.Popen[str], grace: float = 10.0) -> None:
        if process.poll() is not None:
            return
        try:
            group = os.getpgid(process.pid)
            os.killpg(group, signal.SIGINT)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(group, signal.SIGTERM)
            process.wait(timeout=5)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            os.killpg(group, signal.SIGKILL)
            process.wait(timeout=5)

    def status(self, status: str, **values) -> None:
        if status == self._last_status:
            return
        self._last_status = status
        self.event("status", value=status, **values)

    @staticmethod
    def event(event: str, **values) -> None:
        timestamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        fields = " ".join(f"{key}={value}" for key, value in values.items())
        print(f"[{timestamp}] event={event}{' ' if fields else ''}{fields}", flush=True)
