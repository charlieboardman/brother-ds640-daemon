import unittest
from unittest.mock import Mock, patch

from ds640_daemon.presence import is_present, wait_for_state


class PresenceTests(unittest.TestCase):
    def test_only_known_sensor_states_are_accepted(self):
        self.assertTrue(is_present(2))
        self.assertFalse(is_present(3))
        for state in (0, 1, 4, 5, 255):
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                is_present(state)

    def test_insertion_after_empty_polls(self):
        sensor = Mock()
        sensor.present.side_effect = [False, False, True]
        with patch('ds640_daemon.presence.time.sleep'):
            self.assertEqual(0, wait_for_state(sensor))
        self.assertEqual(3, sensor.present.call_count)

    def test_failed_scan_requires_removal(self):
        sensor = Mock()
        sensor.present.side_effect = [True, True, False]
        with patch('ds640_daemon.presence.time.sleep'):
            self.assertEqual(0, wait_for_state(sensor, empty=True))
        self.assertEqual(3, sensor.present.call_count)

    def test_idle_timeout_is_distinct_from_driver_failure(self):
        sensor = Mock()
        sensor.present.return_value = False
        with patch('ds640_daemon.presence.time.monotonic', side_effect=[0, 31]):
            self.assertEqual(2, wait_for_state(sensor))
        sensor.present.side_effect = RuntimeError('disconnected')
        with self.assertRaisesRegex(RuntimeError, 'disconnected'):
            wait_for_state(sensor)
