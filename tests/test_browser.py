import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
URL = 'https://example.test/playback/presentation/2.3/' + 'a'*40 + '-1234567890123'


@unittest.skipUnless(shutil.which('node'), 'Node is needed for the browser adapter test')
class BrowserAdapterTest(unittest.TestCase):
    def run_browser(self, mode, override=None, size=None):
        with tempfile.TemporaryDirectory() as directory:
            args_file = Path(directory) / 'args.json'
            env = {**os.environ, 'ARGS_FILE': str(args_file), 'MOCK_MODE': mode}
            if size is None:
                env.pop('BBB_MP4_SIZE', None)
            else:
                env['BBB_MP4_SIZE'] = size
            if override is None:
                env.pop('BBB_MP4_DURATION', None)
            else:
                env['BBB_MP4_DURATION'] = override
            result = subprocess.run(['node', '-r', str(ROOT / 'tests/mock-browser.cjs'), str(ROOT / 'bbb-mp4.js'), URL],
                                    env=env, text=True, capture_output=True, timeout=10)
            return result, json.loads(args_file.read_text()) if args_file.exists() else None

    def test_waits_for_valid_metadata_and_ffmpeg_before_closing_display(self):
        result, args = self.run_browser('metadata')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('\n11065\n', result.stdout)
        self.assertEqual(args['args'][:3], ['ffmpeg-cmd.sh', '11065', 'a'*40 + '-1234567890123'])
        self.assertNotIn('Xvfb stopped before FFmpeg', result.stderr)

    def test_selected_size_reaches_xvfb_chrome_and_viewport(self):
        for size in ('640x360','854x480','1280x720','1280x800','1920x1080','2560x1440','2560x1080','3440x1440'):
            with self.subTest(size=size):
                result, args = self.run_browser('metadata', size=size)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(size+'x24', args['xvfbOptions']['xvfb_args'])
                self.assertIn('--window-size='+size.replace('x',','), args['chromeOptions']['args'])
                self.assertTrue(args['clearMetrics'])
                self.assertIn('Capture screen: '+size, result.stdout)
        result, args = self.run_browser('metadata')
        self.assertIn('1280x800x24', args['xvfbOptions']['xvfb_args'])
        result, args = self.run_browser('metadata', size='999x999')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(args)

    def test_missing_duration_fails_without_starting_ffmpeg(self):
        result, args = self.run_browser('timeout')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Не удалось получить длительность', result.stderr)
        self.assertIsNone(args)

    def test_override_and_ffmpeg_failure(self):
        result, args = self.run_browser('ffmpeg-fail', '11065')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(args['args'][1], '11065')
        self.assertIn('FFmpeg завершился с кодом 1', result.stderr)
        self.assertNotIn('Xvfb stopped before FFmpeg', result.stderr)

    def test_invalid_override_fails(self):
        result, args = self.run_browser('timeout', 'NaN')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Invalid duration override', result.stderr)
        self.assertIsNone(args)
