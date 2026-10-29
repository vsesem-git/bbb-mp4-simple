import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('web', ROOT / 'web.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)
MEETING_ID = 'f678f4a23ecaf2ac77135e744f2c9da8764c32dd-1793270594439'
URL = 'https://vks.vsesem.ru/playback/presentation/2.3/' + MEETING_ID


class WebTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.old_root = web.ROOT
        web.ROOT = root
        self.addCleanup(setattr, web, 'ROOT', self.old_root)
        shutil.copy(ROOT / 'web.html', root)
        shutil.copy(ROOT / 'convert.sh', root)
        (root / '.env').write_text('COPY_TO_LOCATION=' + shlex.quote(str(root / 'results with spaces')) + '\n')
        self.output = web.output_directory()
        self.output.mkdir()
        bin_dir = root / 'bin'
        bin_dir.mkdir()
        docker = bin_dir / 'docker'
        docker.write_text('''#!/usr/bin/env python3
import os, pathlib, sys, time
args = sys.argv[1:]
root = pathlib.Path(os.environ['MOCK_STATE'])
with (root / 'calls').open('a') as log: log.write(' '.join(args[:4])+'\\n')
if args[0] == 'run':
    name = args[args.index('--name') + 1]
    mount = args[args.index('-v') + 1].split(':/usr/src/app/processed')[0]
    url = args[args.index('--env') + 1][len('REC_URL='):]
    (root / 'received').write_text(url)
    # Simulate Docker taking time to create the named container.
    time.sleep(.2)
    (root / (name + '.stop')).unlink(missing_ok=True)
    (root / (name + '.running')).touch()
    print('5.0', flush=True)
    print('stderr: Output #0, mp4, to output.mp4', flush=True)
    for _ in range(40):
        if (root / (name + '.stop')).exists(): sys.exit(143)
        time.sleep(.05)
    if 'case=fail' in url: sys.exit(1)
    if 'case=stale' not in url:
        mid = url.split('/')[6].split('?')[0].split('#')[0]
        pathlib.Path(mount, mid + '.mp4').write_bytes(b'test mp4')
elif args[0] == 'exec':
    sys.stdout.buffer.write(bytes.fromhex('ffd8ffd9'))
elif args[0] == 'stop':
    if not (root / (args[-1] + '.running')).exists(): sys.exit(1)
    (root / (args[-1] + '.stop')).touch()
elif args[0] == 'rm':
    (root / (args[-1] + '.stop')).touch()
else: sys.exit(1)
''')
        docker.chmod(0o755)
        old_path = os.environ['PATH']
        os.environ['PATH'] = str(bin_dir) + ':' + old_path
        os.environ['MOCK_STATE'] = str(root)
        self.addCleanup(os.environ.__setitem__, 'PATH', old_path)
        self.addCleanup(os.environ.pop, 'MOCK_STATE', None)
        self.server = web.TrustedHTTPServer(('127.0.0.1', 0), web.Handler)
        self.server.jobs = web.Jobs(self.output, root / 'state', screenshot_interval=.2, retry_interval=.1)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def request(self, path, data=None):
        req = Request(self.base + path, data=json.dumps(data).encode() if data is not None else None,
                      headers={'Content-Type': 'application/json'} if data is not None else {})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def json_request(self, path, data=None):
        code, content = self.request(path, data)
        return code, json.loads(content)

    def submit(self, url):
        return self.json_request('/jobs', {'url': url})

    def await_state(self, job_id, states, timeout=7):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code, job = self.json_request('/jobs/' + job_id)
            self.assertEqual(code, 200)
            if job['status'] in states:
                return job
            time.sleep(.05)
        self.fail('job timed out')

    def test_progress_preview_queue_stop_recreate_and_delete(self):
        self.assertEqual(self.request('/')[0], 200)
        self.assertTrue(self.server.verify_request(None, ('192.168.1.2', 10)))
        self.assertFalse(self.server.verify_request(None, ('8.8.8.8', 10)))
        self.assertEqual(self.submit('https://example.org/../../bad')[0], 400)
        url = URL + '?token=a&x=b#test'
        code, created = self.submit(url)
        self.assertEqual(code, 202)
        job_id = created['id']
        self.assertEqual(self.submit(url)[0], 400)
        second_url = URL.replace(MEETING_ID, 'a' * 40 + '-1234567890123')
        second_id = self.submit(second_url)[1]['id']
        self.assertEqual(self.json_request('/jobs/' + second_id)[1]['status'], 'queued')
        self.assertEqual(self.json_request('/jobs/' + second_id + '/stop', {})[0], 202)
        self.assertEqual(self.await_state(second_id, {'stopped'})['status'], 'stopped')
        running = self.await_state(job_id, {'running'})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            running = self.json_request('/jobs/' + job_id)[1]
            if running['preview'] and running['progress'] is not None:
                break
            time.sleep(.05)
        self.assertIsNotNone(running['progress'])
        self.assertIsNotNone(running['remaining'])
        self.assertEqual(self.request(running['preview'])[1], bytes.fromhex('ffd8ffd9'))
        self.assertEqual(self.json_request('/jobs/' + job_id + '/delete', {})[0], 400)
        job = self.await_state(job_id, {'done'})
        self.assertGreaterEqual((web.ROOT / 'calls').read_text().count('exec bbb-mp4-' + job_id), 2)
        self.assertEqual((web.ROOT / 'received').read_text(), url)
        self.assertEqual(job['progress'], 100)
        self.assertEqual(self.request(job['download']), (200, b'test mp4'))
        self.assertEqual(self.request('/download/../.env')[0], 404)
        self.assertEqual(len(self.json_request('/jobs')[1]), 2)
        self.assertEqual(self.json_request('/jobs/' + job_id + '/recreate', {})[0], 202)
        self.await_state(job_id, {'running'})
        self.assertEqual(self.json_request('/jobs/' + job_id + '/stop', {})[0], 202)
        self.await_state(job_id, {'stopped'})
        # A cancelled recreation does not remove the previously completed file.
        self.assertTrue((self.output / (MEETING_ID + '.mp4')).exists())
        self.assertEqual(self.request(self.json_request('/jobs/' + job_id)[1]['download'])[1], b'test mp4')
        self.assertEqual(self.json_request('/jobs/' + job_id + '/delete', {})[0], 202)
        self.assertFalse((self.output / (MEETING_ID + '.mp4')).exists())
        self.assertFalse(list(self.output.glob('.bbb-mp4-*')))
        self.assertEqual(self.json_request('/jobs/' + job_id)[0], 404)
        self.assertIn('stop --time 5 bbb-mp4-' + job_id, (web.ROOT / 'calls').read_text())

    def test_failure_stale_output_and_persistence_import(self):
        # Existing files from the old in-memory version remain visible.
        old_id = 'b' * 40 + '-1234567890123'
        (self.output / (old_id + '.mp4')).write_bytes(b'old')
        self.server.jobs.import_files()
        old = next(j for j in self.server.jobs.list() if j['meeting_id'] == old_id)
        self.assertIsNone(old['url'])
        self.assertEqual(self.request(old['download'])[1], b'old')
        self.assertEqual(self.json_request('/jobs/' + old['id'] + '/recreate', {})[0], 400)
        code, created = self.submit(URL + '?case=fail')
        self.assertEqual(code, 202)
        job_id = created['id']
        self.assertEqual(self.await_state(job_id, {'failed'})['status'], 'failed')
        # A pre-existing file must not make a no-output run successful.
        (self.output / (MEETING_ID + '.mp4')).write_bytes(b'old result')
        with self.server.jobs.condition:
            self.server.jobs.jobs[job_id]['url'] = URL + '?case=stale'
            self.server.jobs.save()
        self.assertEqual(self.json_request('/jobs/' + job_id + '/recreate', {})[0], 202)
        self.assertEqual(self.await_state(job_id, {'failed'})['status'], 'failed')
        reloaded = web.Jobs(self.output, web.ROOT / 'state')
        self.assertEqual(reloaded.get(job_id)['status'], 'failed')
        self.assertIsNotNone(reloaded.get(old['id']))
        self.assertEqual((web.ROOT / 'state' / 'jobs.json').stat().st_mode & 0o777, 0o600)

    def test_stop_before_container_is_created(self):
        job_id = self.submit(URL)[1]['id']
        self.await_state(job_id, {'running'})
        self.assertEqual(self.json_request('/jobs/' + job_id + '/stop', {})[0], 202)
        self.assertEqual(self.await_state(job_id, {'stopped'})['status'], 'stopped')
        self.assertFalse((self.output / (MEETING_ID + '.mp4')).exists())

    def test_recovery_stops_orphaned_container(self):
        job_id = '1' * 32
        state = web.ROOT / 'recovery'
        state.mkdir()
        queued_id = '3' * 32
        other_id = 'a' * 40 + '-1234567890123'
        (state / 'jobs.json').write_text(json.dumps([
            {'id':job_id, 'meeting_id':MEETING_ID, 'url':URL,
             'status':'running','run_id':'2'*32,'created':time.time(), 'duration':5,
             'capture_started':time.time(), 'screenshot_time':None, 'error':None},
            {'id':queued_id, 'meeting_id':other_id, 'url':URL.replace(MEETING_ID, other_id),
             'status':'queued','run_id':'4'*32,'created':time.time(), 'queued_at':time.time_ns(),
             'duration':None, 'capture_started':None, 'screenshot_time':None, 'error':None}]))
        workspace = self.output / ('.bbb-mp4-' + job_id + '-' + '2'*32)
        workspace.mkdir()
        (workspace / 'partial.mp4').write_bytes(b'incomplete')
        jobs = web.Jobs(self.output, state)
        self.assertFalse(workspace.exists())
        self.assertEqual(jobs.get(job_id)['status'], 'stopped')
        self.assertTrue((web.ROOT / ('bbb-mp4-' + job_id + '.stop')).exists())
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and jobs.get(queued_id)['status'] != 'done':
            time.sleep(.05)
        self.assertEqual(jobs.get(queued_id)['status'], 'done')


if __name__ == '__main__':
    unittest.main()
