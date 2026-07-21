from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from PIL import Image

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

    def test_auto_document_size_is_not_sent(self) -> None:
        command = self.daemon(ScannerConfig())._scanimage_command(Path("/tmp/job"), 0)
        self.assertNotIn("--AutoDocumentSize=yes", command)

    def test_version_1_1_0_auto_size_config_is_safely_ignored(self) -> None:
        scanner = ScannerConfig(auto_document_size=True)
        command = self.daemon(scanner)._scanimage_command(Path("/tmp/job"), 0)
        self.assertNotIn("--AutoDocumentSize=yes", command)


if __name__ == "__main__":
    unittest.main()
