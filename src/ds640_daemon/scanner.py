from __future__ import annotations

import datetime as dt
import fcntl
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from threading import Event

import img2pdf
from PIL import Image, ImageFile

from .config import Config
from .runtime import discover_usb, ensure_runtime, popen_runtime


class AlreadyRunningError(RuntimeError):
    pass


class ScanDaemon:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.paths = ensure_runtime(config)
        self._last_status: str | None = None

    def run(self, stop_event: Event, *, max_pages: int = 0) -> int:
        self.config.output.path.mkdir(parents=True, exist_ok=True)
        lock_handle = self._lock()
        del lock_handle  # The descriptor remains open for this process lifetime.
        self.cleanup_temporary_files()
        total_pages = self.recover_jobs()
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

            remaining = max_pages - total_pages if max_pages else 0
            pages, exit_code = self._run_session(stop_event, max_pages=remaining)
            total_pages += pages
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

    def _run_session(self, stop_event: Event, *, max_pages: int = 0) -> tuple[int, int]:
        session_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        job = self.paths.jobs / f"session-{session_id}"
        job.mkdir(parents=True, exist_ok=False)
        command = self._scanimage_command(job, max_pages)
        stdio = subprocess.DEVNULL if self.config.runtime.quiet else None
        process = popen_runtime(
            self.config,
            command,
            atcn=True,
            stdout=stdio,
            stderr=stdio,
        )
        stable: dict[str, tuple[int, int, float]] = {}
        processed: set[str] = set()
        pages = 0

        try:
            while not stop_event.is_set():
                for image in self._images(job):
                    identity = self._identity(image)
                    if identity in processed:
                        if not self.config.output.keep_images and not image.name.endswith(".part"):
                            image.unlink(missing_ok=True)
                        continue
                    if image.name.endswith(".part") and not self._part_ready(image, stable):
                        continue
                    output = self._deliver(image, job, session_id)
                    processed.add(identity)
                    pages += 1
                    self.event("page_saved", file=output, page=pages)
                    if max_pages and pages >= max_pages:
                        self._terminate(process)
                        break
                if max_pages and pages >= max_pages:
                    break
                if process.poll() is not None:
                    break
                if stop_event.wait(self.config.scanner.poll_seconds):
                    self._terminate(process)
                    break

            if stop_event.is_set():
                self._terminate(process)

            for image in self._images(job):
                identity = self._identity(image)
                if identity in processed:
                    if not self.config.output.keep_images and not image.name.endswith(".part"):
                        image.unlink(missing_ok=True)
                    continue
                if image.name.endswith(".part") and not self._recoverable(image):
                    continue
                output = self._deliver(image, job, session_id)
                processed.add(identity)
                pages += 1
                self.event("page_saved_after_stop", file=output, page=pages)

            return pages, process.returncode if process.returncode is not None else 0
        finally:
            self._terminate(process)
            self._clean_delivered(job)
            self._remove_empty(job)

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
        if max_pages:
            command.append(f"--batch-count={max_pages}")
        if scanner.mode:
            command.extend(["--mode", scanner.mode])
        if scanner.source:
            command.extend(["--source", scanner.source])
        if scanner.auto_document_size:
            command.append("--AutoDocumentSize=yes")
        command.extend(scanner.extra_args)
        return command

    def _images(self, job: Path) -> list[Path]:
        extension = self.config.scanner.image_format
        images = list(job.glob(f"*.{extension}")) + list(job.glob(f"*.{extension}.part"))
        return sorted(images, key=lambda path: self._identity(path))

    @staticmethod
    def _identity(image: Path) -> str:
        return image.name[:-5] if image.name.endswith(".part") else image.name

    def _part_ready(self, image: Path, state: dict[str, tuple[int, int, float]]) -> bool:
        try:
            stat = image.stat()
        except FileNotFoundError:
            return False
        identity = self._identity(image)
        signature = (stat.st_size, stat.st_mtime_ns)
        now = time.monotonic()
        previous = state.get(identity)
        if previous is None or previous[:2] != signature:
            state[identity] = (*signature, now)
            return False
        if now - previous[2] < self.config.scanner.part_settle_seconds:
            return False
        return self._recoverable(image)

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
