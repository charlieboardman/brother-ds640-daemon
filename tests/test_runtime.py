from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ds640_daemon.config import Config
from ds640_daemon.runtime import discover_usb


class DiscoveryTests(unittest.TestCase):
    def test_discovers_scanner_without_hardcoded_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / "9-7"
            device.mkdir()
            values = {
                "idVendor": "04f9\n",
                "idProduct": "0468\n",
                "busnum": "9\n",
                "devnum": "42\n",
                "manufacturer": "Brother\n",
                "product": "DS-640\n",
            }
            for name, value in values.items():
                (device / name).write_text(value, encoding="utf-8")

            found = discover_usb(Config(), root)

            self.assertEqual(1, len(found))
            self.assertEqual(Path("/dev/bus/usb/009/042"), found[0].node)
            self.assertIn("Brother DS-640", found[0].description)

    def test_ignores_other_usb_devices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / "1-1"
            device.mkdir()
            (device / "idVendor").write_text("1234", encoding="utf-8")
            (device / "idProduct").write_text("0468", encoding="utf-8")
            self.assertEqual([], discover_usb(Config(), root))


if __name__ == "__main__":
    unittest.main()
