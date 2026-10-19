from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from ds640_daemon.config import Config, ScannerConfig
from ds640_daemon.scanner import ScanDaemon


class ImageRecoveryTests(unittest.TestCase):
    def test_header_only_file_is_not_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "page.png.part"
            path.write_bytes(b"\x89PNG\r\n\x1a\n")
            self.assertFalse(ScanDaemon._recoverable(path))

    def test_truncated_png_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "page.png.part"
            pixels = bytes((index * 73 + index // 17) % 256 for index in range(128 * 128 * 3))
            Image.frombytes("RGB", (128, 128), pixels).save(path, format="PNG", dpi=(300, 300))
            data = path.read_bytes()
            path.write_bytes(data[: int(len(data) * 0.9)])
            self.assertTrue(ScanDaemon._recoverable(path))

    def test_long_paper_trim_removes_scanner_background(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "page.png"
            picture = Image.new("RGB", (600, 900), (192, 192, 192))
            draw = ImageDraw.Draw(picture)
            draw.rectangle((20, 10, 220, 890), fill="white")
            picture.save(image, format="PNG", dpi=(300, 300))

            trimmed = ScanDaemon._trimmed_image(image, root, "page")

            self.assertIsNotNone(trimmed)
            assert trimmed is not None
            with Image.open(trimmed) as result:
                self.assertLess(result.width, 300)
                self.assertGreater(result.height, 800)
            trimmed.unlink()

    def test_long_paper_trim_skips_full_width_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "page.png"
            Image.new("RGB", (600, 900), "white").save(
                image, format="PNG", dpi=(300, 300)
            )

            self.assertIsNone(ScanDaemon._trimmed_image(image, root, "page"))

    def test_long_paper_trim_skips_dark_corner_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "page.png"
            picture = Image.new("RGB", (600, 900), "white")
            draw = ImageDraw.Draw(picture)
            draw.rectangle((500, 0, 599, 100), fill=(20, 20, 20))
            picture.save(image, format="PNG", dpi=(300, 300))

            self.assertIsNone(ScanDaemon._trimmed_image(image, root, "page"))

    def test_recovered_pdf_is_atomic_and_preserves_dpi(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "page.png.part"
            output = root / "scan.pdf"
            Image.new("RGB", (255, 420), "white").save(image, format="PNG", dpi=(300, 300))

            ScanDaemon._image_to_pdf(image, output, truncated=True)

            self.assertTrue(output.read_bytes().startswith(b"%PDF"))
            self.assertEqual([], list(root.glob(".*.tmp")))
            match = re.search(
                rb"/MediaBox\s*\[\s*0\s+0\s+([0-9.]+)\s+([0-9.]+)\s*\]",
                output.read_bytes(),
            )
            self.assertIsNotNone(match)
            assert match is not None
            self.assertAlmostEqual(61.2, float(match.group(1)), places=1)
            self.assertAlmostEqual(100.8, float(match.group(2)), places=1)


class ScanCommandTests(unittest.TestCase):
    @staticmethod
    def daemon(scanner: ScannerConfig) -> ScanDaemon:
        daemon = ScanDaemon.__new__(ScanDaemon)
        daemon.config = Config(scanner=scanner)
        return daemon

    def test_auto_document_size_is_sent(self) -> None:
        command = self.daemon(ScannerConfig())._scanimage_command(Path("/tmp/job"), 0)
        self.assertIn("--AutoDocumentSize=yes", command)

    def test_auto_document_size_can_be_disabled(self) -> None:
        scanner = ScannerConfig(auto_document_size=False)
        command = self.daemon(scanner)._scanimage_command(Path("/tmp/job"), 0)
        self.assertIn("--AutoDocumentSize=no", command)

    def test_long_paper_adds_scan_height(self) -> None:
        scanner = ScannerConfig(long_paper=True)
        command = self.daemon(scanner)._scanimage_command(Path("/tmp/job"), 0)
        self.assertIn("-y", command)
        self.assertIn("1828.8", command)

    def test_normal_mode_does_not_add_long_paper_height(self) -> None:
        command = self.daemon(ScannerConfig())._scanimage_command(Path("/tmp/job"), 0)
        self.assertNotIn("-y", command)


if __name__ == "__main__":
    unittest.main()
