from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ds640_daemon.config import Config, ScannerConfig, render_config


class ConfigTests(unittest.TestCase):
    def test_auto_size_resolution_and_timeout_limits(self):
        with self.assertRaisesRegex(ValueError, '400 DPI'):
            Config(scanner=ScannerConfig(resolution=600)).validate()
        Config(scanner=ScannerConfig(resolution=600, auto_document_size=False)).validate()
        with self.assertRaisesRegex(ValueError, 'timeout'):
            Config(scanner=ScannerConfig(scan_timeout_seconds=0)).validate()

    def test_rendered_default_config_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(render_config("~/My Scans"), encoding="utf-8")
            config = Config.load(path)

            self.assertEqual(300, config.scanner.resolution)
            self.assertTrue(config.scanner.auto_document_size)
            self.assertEqual("~/My Scans", config.output.directory)
            self.assertEqual(("png", 5.0), (config.scanner.image_format, config.scanner.part_settle_seconds))

    def test_long_paper_defaults_and_validation(self):
        config = Config()
        self.assertFalse(config.scanner.long_paper)
        self.assertEqual(24, config.scanner.long_paper_paper_type)
        self.assertAlmostEqual(1828.8, config.scanner.max_document_length_mm)
        self.assertAlmostEqual(215.9, config.scanner.long_paper_width_mm)
        Config(scanner=ScannerConfig(long_paper=True)).validate()
        Config(scanner=ScannerConfig(long_paper=True, long_paper_paper_type=23,
                                     long_paper_width_mm=107.95)).validate()
        with self.assertRaisesRegex(ValueError, 'long_paper_paper_type'):
            Config(scanner=ScannerConfig(long_paper_paper_type=25)).validate()
        with self.assertRaisesRegex(ValueError, 'long_paper_width_mm'):
            Config(scanner=ScannerConfig(long_paper_paper_type=23)).validate()
        with self.assertRaisesRegex(ValueError, 'max_document_length_mm'):
            Config(scanner=ScannerConfig(max_document_length_mm=2000)).validate()
        with self.assertRaisesRegex(ValueError, '300 DPI'):
            Config(scanner=ScannerConfig(long_paper=True, resolution=600,
                                         auto_document_size=False)).validate()

    def test_unknown_configuration_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("[output]\nunknown = true\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unknown configuration keys"):
                Config.load(path)

    def test_existing_config_gets_safe_auto_document_size_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[output]\ndirectory = "~/Scans/Inbox"\n', encoding="utf-8")
            config = Config.load(path)

            self.assertTrue(config.scanner.auto_document_size)

    def test_output_path_expands_home_and_environment(self) -> None:
        with patch.dict(os.environ, {"SCAN_ROOT": "/tmp/scanner-test"}):
            config = Config(output=Config().output.__class__(directory="$SCAN_ROOT/inbox"))
            self.assertEqual(Path("/tmp/scanner-test/inbox"), config.output.path)


if __name__ == "__main__":
    unittest.main()
