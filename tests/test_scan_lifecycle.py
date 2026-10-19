from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from ds640_daemon.config import Config, OutputConfig, RuntimeConfig, ScannerConfig
from ds640_daemon.scanner import ScanDaemon


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.daemon = ScanDaemon.__new__(ScanDaemon)
        self.daemon.config = Config(output=OutputConfig(directory=str(self.root / 'out')),
                                    runtime=RuntimeConfig(state_directory=str(self.root)))
        self.daemon.paths = SimpleNamespace(jobs=self.root / 'jobs')
        self.daemon.paths.jobs.mkdir()
        self.daemon._last_status = None
        self.daemon.event = Mock()

    def fake_scan(self, *, partial=False, code=0):
        def spawn(config, command, **kwargs):
            self.assertNotIn('atcn', kwargs)
            self.assertIn('--batch-count=1', command)
            self.assertIn('--AutoDocumentSize=yes', command)
            template = next(arg.split('=', 1)[1] for arg in command if arg.startswith('--batch='))
            path = Path(template % 1)
            if partial:
                path = Path(str(path) + '.part')
            Image.new('RGB', (161, 125), 'white').save(path, format='PNG', dpi=(300, 300))
            return Mock(poll=Mock(return_value=code), returncode=code)
        return spawn

    def test_complete_normal_scan_publishes_once_and_cleans_job(self):
        with patch('ds640_daemon.scanner.popen_runtime', side_effect=self.fake_scan()):
            self.assertEqual((1, 0), self.daemon._run_session(Event()))
        self.assertEqual(1, len(list((self.root / 'out').glob('*.pdf'))))
        self.assertEqual([], list(self.daemon.paths.jobs.iterdir()))
        self.assertEqual(0, self.daemon.recover_jobs())

    def test_partial_new_scan_is_retained_and_never_published_by_recovery(self):
        with patch('ds640_daemon.scanner.popen_runtime', side_effect=self.fake_scan(partial=True, code=7)):
            self.assertEqual((0, 7), self.daemon._run_session(Event()))
        self.assertEqual(0, self.daemon.recover_jobs())
        self.assertEqual(1, len(list(self.daemon.paths.jobs.glob('session-*/*.part'))))
        self.assertFalse(list((self.root / 'out').glob('*.pdf')))

    def test_completed_page_is_recovered_after_crash_before_delivery(self):
        job = self.daemon.paths.jobs / 'session-crash'
        job.mkdir()
        (job / '.normal-scan').touch()
        Image.new('RGB', (100, 80), 'white').save(job / 'page-0001.png', dpi=(300, 300))
        self.assertEqual(1, self.daemon.recover_jobs())
        self.assertEqual(0, self.daemon.recover_jobs())

    def test_long_paper_scan_passes_shim_and_environment(self):
        shim = self.root / 'libds640_longpaper.so'
        shim.touch()
        self.daemon.config = Config(
            scanner=ScannerConfig(long_paper=True),
            output=OutputConfig(directory=str(self.root / 'out')),
            runtime=RuntimeConfig(state_directory=str(self.root),
                                  long_paper_library=str(shim)),
        )
        calls = []

        def spawn(config, command, **kwargs):
            calls.append((command, kwargs))
            template = next(arg.split('=', 1)[1] for arg in command
                            if arg.startswith('--batch='))
            path = Path(template % 1)
            Image.new('RGB', (600, 900), 'white').save(path, format='PNG', dpi=(300, 300))
            return Mock(poll=Mock(return_value=0), returncode=0)

        with patch('ds640_daemon.scanner.popen_runtime', side_effect=spawn):
            self.assertEqual((1, 0), self.daemon._run_session(Event()))

        command, kwargs = calls[0]
        self.assertEqual(shim, kwargs['preload'])
        self.assertEqual('1', kwargs['environment']['DS640_LONG_PAPER_FORCE'])
        self.assertEqual('24', kwargs['environment']['DS640_LONG_PAPER_TYPE'])
        self.assertIn('-y', command)
        self.assertIn('1828.8', command)

    def test_stop_terminates_worker_without_waiting_for_driver(self):
        stop = Event()
        stop.set()
        process = Mock(poll=Mock(return_value=None))
        with patch.object(self.daemon, '_terminate') as terminate:
            self.assertEqual(130, self.daemon._wait_process(process, stop, timeout=60))
        terminate.assert_called_once_with(process)

    def test_hung_worker_is_terminated_on_deadline(self):
        process = Mock(poll=Mock(return_value=None))
        stop = Mock(wait=Mock(return_value=False))
        with patch('ds640_daemon.scanner.time.monotonic', side_effect=[0, 61]), \
                patch.object(self.daemon, '_terminate') as terminate:
            self.assertEqual(124, self.daemon._wait_process(process, stop, timeout=60))
        terminate.assert_called_once_with(process)

    def test_failed_scan_must_clear_feeder_before_retry(self):
        stop = Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False))
        with patch.object(self.daemon, '_lock'), \
                patch('ds640_daemon.scanner.discover_usb', return_value=[SimpleNamespace(node='/dev/fake')]), \
                patch('ds640_daemon.scanner.os.access', return_value=True), \
                patch.object(self.daemon, '_wait_for_paper', return_value=0) as presence, \
                patch.object(self.daemon, '_run_session', side_effect=[(0, 7), (1, 0)]):
            self.assertEqual(1, self.daemon.run(stop, max_pages=1))
        self.assertEqual([False, True, False], [call.kwargs['require_empty'] for call in presence.call_args_list])

    def test_two_successful_insertions_return_to_sensor(self):
        stop = Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False))
        with patch.object(self.daemon, '_lock'), \
                patch('ds640_daemon.scanner.discover_usb', return_value=[SimpleNamespace(node='/dev/fake')]), \
                patch('ds640_daemon.scanner.os.access', return_value=True), \
                patch.object(self.daemon, '_wait_for_paper', side_effect=[2, 0, 0]) as presence, \
                patch.object(self.daemon, '_run_session', return_value=(1, 0)) as scan:
            self.assertEqual(2, self.daemon.run(stop, max_pages=2))
        self.assertEqual(3, presence.call_count)
        self.assertEqual(2, scan.call_count)
