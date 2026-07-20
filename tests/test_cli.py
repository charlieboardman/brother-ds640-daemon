from __future__ import annotations

import unittest
from pathlib import Path

from ds640_daemon.cli import _render_service


class ServiceTests(unittest.TestCase):
    def test_service_runs_unprivileged_daemon_and_restarts_on_failure(self) -> None:
        unit = _render_service(Path("/home/user/.local/bin/ds640"), Path("/home/user/config.toml"))
        self.assertIn(" run", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("KillMode=control-group", unit)
        self.assertNotIn("User=root", unit)


if __name__ == "__main__":
    unittest.main()
