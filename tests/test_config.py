from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ds640_daemon.config import Config, render_config


class ConfigTests(unittest.TestCase):
    def test_rendered_default_config_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(render_config("~/My Scans"), encoding="utf-8")
            config = Config.load(path)

            self.assertEqual(300, config.scanner.resolution)
            self.assertEqual("~/My Scans", config.output.directory)
            self.assertEqual(("png", 5.0), (config.scanner.image_format, config.scanner.part_settle_seconds))

    def test_unknown_configuration_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("[output]\nunknown = true\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unknown configuration keys"):
                Config.load(path)

    def test_output_path_expands_home_and_environment(self) -> None:
        with patch.dict(os.environ, {"SCAN_ROOT": "/tmp/scanner-test"}):
            config = Config(output=Config().output.__class__(directory="$SCAN_ROOT/inbox"))
            self.assertEqual(Path("/tmp/scanner-test/inbox"), config.output.path)


if __name__ == "__main__":
    unittest.main()
