import importlib.util
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('web', ROOT / 'web.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)
MEETING_ID = 'f678f4a23ecaf2ac77135e744f2c9da8764c32dd-1793270594439'
URL = 'https://vks.vsesem.ru/playback/presentation/2.3/' + MEETING_ID


class ProgressParserTest(unittest.TestCase):
    def test_complete_frame_block_and_partial_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'progress'
            self.assertIsNone(web.ffmpeg_progress(path))
            path.write_bytes(b'frame=10\nout_time_ms=2000000\nspeed=0.5x\nprogress=continue\nframe=20\nout_time_us=')
            result = web.ffmpeg_progress(path)
            self.assertEqual(result, {'frame':10, 'time_us':2000000, 'speed':'0.5x'})
            with path.open('ab') as f:
                f.write(b'3000000\nspeed=0.75x\nprogress=end\n')
            self.assertEqual(web.ffmpeg_progress(path), {'frame':20, 'time_us':3000000, 'speed':'0.75x'})
            path.write_bytes(b'frame=NaN\nout_time_us=N/A\nprogress=continue\n')
            self.assertIsNone(web.ffmpeg_progress(path))
            path.write_bytes(b'x' * 70000 + b'\nprogress=continue\nframe=31\nout_time_us=N/A\nprogress=continue\n')
            self.assertEqual(web.ffmpeg_progress(path)['frame'], 31)
            self.assertIsNone(web.ffmpeg_progress(path)['time_us'])

class ScreenshotCommandTest(unittest.TestCase):
    def test_preview_captures_the_selected_screen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(sock.close)
            sock.bind(str(root / 'X112'))
            ffmpeg = root / 'ffmpeg'
            ffmpeg.write_text('#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n')
            ffmpeg.chmod(0o755)
            command = web.CAPTURE.replace('/tmp/.X11-unix', str(root))
            env = {**os.environ, 'PATH':str(root)+':'+os.environ['PATH'], 'BBB_MP4_SIZE':'2560x1440'}
            result = subprocess.run(['sh', '-c', command], env=env, capture_output=True, text=True, check=True)
            args = json.loads(result.stdout)
            self.assertEqual(args[args.index('-video_size')+1], '2560x1440')
            self.assertEqual(args[args.index('-i')+1], ':112')
            self.assertNotIn('-vf', args)

class WebTest(unittest.TestCase):
    def setUp(self):
        old_delay = web.RETRY_DELAY
        web.RETRY_DELAY = .05
        self.addCleanup(setattr, web, 'RETRY_DELAY', old_delay)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.old_root = web.ROOT
        web.ROOT = root
        self.addCleanup(setattr, web, 'ROOT', self.old_root)
        shutil.copy(ROOT / 'web.html', root)
        shutil.copy(ROOT / 'convert.sh', root)
        shutil.copy(ROOT / 'ffmpeg-cmd.sh', root)
        shutil.copy(ROOT / 'bbb-mp4.js', root)
        (root / '.env').write_text('COPY_TO_LOCATION=' + shlex.quote(str(root / 'results with spaces')) + '\n')
        self.output = web.output_directory()
        self.output.mkdir()
        bin_dir = root / 'bin'
        bin_dir.mkdir()
        docker = bin_dir / 'docker'
        docker.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
root = pathlib.Path(os.environ['MOCK_STATE'])
with (root / 'calls').open('a') as log: log.write(' '.join(args[:4])+'\\n')
if args[0] == 'run' and '--entrypoint' in args:
    with (root / 'inspect-args').open('a') as log: log.write(json.dumps(args)+'\\n')
    name = args[args.index('--name') + 1].replace('bbb-probe-', 'bbb-mp4-')
    run_args = json.loads((root / (name + '.args')).read_text())
    url = next((v[len('REC_URL='):] for v in run_args if v.startswith('REC_URL=')), None)
    if url is None: url = (root / 'received').read_text()
    program = args[args.index('--entrypoint') + 1]
    if program == 'ffmpeg':
        if 'case=decodeerror' in url: print('corrupt video frame', file=sys.stderr); sys.exit(1)
        if 'case=warnings' in url: print('black_duration:25.0 silence_duration:25.0', file=sys.stderr)
        sys.exit(0)
    if 'case=probeerror' in url:
        print('invalid mp4', file=sys.stderr); sys.exit(1)
    if 'case=slowprobe' in url:
        name = args[args.index('--name') + 1]
        (root / (name + '.running')).touch()
        for _ in range(150):
            if (root / (name + '.stop')).exists(): sys.exit(143)
            time.sleep(.05)
    size = next((v.split('=',1)[1] for v in run_args if v.startswith('BBB_MP4_SIZE=')), '1280x800')
    width, height = map(int, size.split('x'))
    if 'case=wrongsize' in url: width += 2
    codec = next((v.split('=',1)[1] for v in run_args if v.startswith('BBB_MP4_CODEC=')), 'h264')
    codec = 'hevc' if codec == 'h265' else 'h264'
    if 'case=wrongcodec' in url: codec = 'mpeg4'
    video = {'codec_type':'video', 'codec_name':codec, 'width':width, 'height':height}
    types = [video] if 'case=noaudio' in url else [video, {'codec_type':'audio'}]
    duration = '1.0' if 'case=short' in url else '5.0'
    print(json.dumps({'format':{'format_name':'mov,mp4,m4a,3gp,3g2,mj2','duration':duration}, 'streams':types}))
elif args[0] == 'run':
    name = args[args.index('--name') + 1]
    mount = next(x[:-len(':/usr/src/app/processed')] for x in args if x.endswith(':/usr/src/app/processed'))
    url = next(x[len('REC_URL='):] for x in args if x.startswith('REC_URL='))
    (root / (name + '.args')).write_text(json.dumps(args))
    (root / 'received').write_text(url)
    # Simulate Docker taking time to create the named container.
    time.sleep(.2)
    (root / (name + '.stop')).unlink(missing_ok=True)
    (root / (name + '.running')).touch()
    if 'case=hang' in url:
        while not (root / (name + '.stop')).exists(): time.sleep(.05)
        sys.exit(143)
    print('5.0', flush=True)
    print('stderr: Output #0, mp4, to output.mp4', flush=True)
    progress = pathlib.Path(mount, '.ffmpeg-progress')
    if 'case=stallprogress' in url or 'case=slowprogress' in url:
        frame = 0
        while not (root / (name + '.stop')).exists():
            with progress.open('a') as f:
                f.write('frame=%d\\nout_time_us=%d\\nspeed=0.1x\\nprogress=continue\\n' % (frame, frame * 33333))
            if 'case=slowprogress' in url: frame += 1
            time.sleep(.1)
        sys.exit(143)
    for n in range(20):
        if (root / (name + '.stop')).exists():
            (root / (name + '.running')).unlink(missing_ok=True); sys.exit(143)
        with progress.open('a') as f:
            frame = 150 if 'case=flushprogress' in url else n * 3
            f.write('frame=%d\\nout_time_us=%d\\nspeed=1x\\nprogress=continue\\n' % (frame, n * 100000))
        time.sleep(.05)
    (root / (name + '.running')).unlink(missing_ok=True)
    count_file = root / (name + '.attempts')
    attempt = int(count_file.read_text()) + 1 if count_file.exists() else 1
    count_file.write_text(str(attempt))
    if 'case=notfound' in url: print('Recording not found!', flush=True); sys.exit(1)
    if 'case=transient' in url and attempt == 1: print('Chrome crashed', flush=True); sys.exit(1)
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

    def submit(self, url, settings=None, replace_confirmed=False):
        return self.json_request('/jobs', {'url': url, 'settings': settings or {}, 'replace_confirmed':replace_confirmed})

    def await_state(self, job_id, states, timeout=12):
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
        self.assertEqual(job['file_size'], len(b'test mp4'))
        self.assertEqual(job['file_duration'], 5.0)
        self.assertEqual(job['settings']['codec'], 'h264')
        self.assertIsNone(job['settings']['video_bitrate'])
        self.assertEqual(self.request(job['download']), (200, b'test mp4'))
        self.assertEqual(self.request('/download/../.env')[0], 404)
        self.assertEqual(len(self.json_request('/jobs')[1]), 2)
        self.assertEqual(self.json_request('/jobs/' + job_id + '/recreate', {})[0], 202)
        self.await_state(job_id, {'running'})
        self.assertEqual(self.json_request('/jobs/' + job_id + '/stop', {})[0], 202)
        self.await_state(job_id, {'stopped'})
        # The old file was deleted immediately when recreation was requested.
        self.assertFalse((self.output / (MEETING_ID + '.mp4')).exists())
        self.assertIsNone(self.json_request('/jobs/' + job_id)[1]['download'])
        self.assertEqual(self.json_request('/jobs/' + job_id + '/delete', {})[0], 202)
        self.assertFalse((self.output / (MEETING_ID + '.mp4')).exists())
        self.assertFalse(list(self.output.glob('.bbb-mp4-*')))
        self.assertEqual(self.json_request('/jobs/' + job_id)[0], 404)
        self.assertIn('stop --time 5 bbb-mp4-' + job_id, (web.ROOT / 'calls').read_text())

    def test_forced_runs_parallel_with_limit_and_independent_stop(self):
        def url(letter):
            return URL.replace(MEETING_ID, letter*40 + '-1234567890123') + '?case=hang'
        ids = [self.submit(url(letter))[1]['id'] for letter in 'abcde']
        self.assertEqual(self.await_state(ids[0], {'running'})['status'], 'running')
        self.assertEqual(self.json_request('/jobs/' + ids[0] + '/force', {})[0], 400)
        self.assertEqual(self.json_request('/jobs/' + ids[1] + '/force', {})[0], 202)
        self.assertEqual(self.json_request('/jobs/' + ids[2] + '/force', {})[0], 202)
        self.assertTrue(self.await_state(ids[1], {'running'})['forced'])
        self.assertTrue(self.await_state(ids[2], {'running'})['forced'])
        self.assertEqual(self.json_request('/jobs/' + ids[3])[1]['status'], 'queued')
        code, error = self.json_request('/jobs/' + ids[3] + '/force', {})
        self.assertEqual(code, 400)
        self.assertIn('3', error['error'])
        self.assertEqual(self.json_request('/jobs/' + ids[1] + '/stop', {})[0], 202)
        self.await_state(ids[1], {'stopped'})
        self.assertEqual(self.json_request('/jobs/' + ids[0])[1]['status'], 'running')
        self.assertEqual(self.json_request('/jobs/' + ids[2])[1]['status'], 'running')
        # Normal queued jobs do not start just because a forced slot is free.
        self.assertEqual(self.json_request('/jobs/' + ids[3])[1]['status'], 'queued')
        self.assertEqual(self.json_request('/jobs/' + ids[3] + '/force', {})[0], 202)
        self.assertTrue(self.await_state(ids[3], {'running'})['forced'])
        # The next ordinary job starts after the ordinary worker is stopped.
        self.assertEqual(self.json_request('/jobs/' + ids[0] + '/stop', {})[0], 202)
        self.await_state(ids[0], {'stopped'})
        self.assertFalse(self.await_state(ids[4], {'running'}).get('forced', False))
        active = [j for j in self.json_request('/jobs')[1] if j['status'] in ('running','stopping')]
        self.assertEqual(len(active), 3)
        for job_id in (ids[2], ids[3], ids[4]):
            self.assertEqual(self.json_request('/jobs/' + job_id + '/stop', {})[0], 202)
        for job_id in (ids[2], ids[3], ids[4]):
            self.await_state(job_id, {'stopped'})
        self.assertIn('stop --time 5 bbb-mp4-' + ids[1], (web.ROOT / 'calls').read_text())

    def test_force_is_rejected_during_maintenance(self):
        jobs = self.server.jobs
        with jobs.condition:
            job_id = jobs.submit(URL + '?case=hang')
            jobs.set_maintenance(True)
        self.assertEqual(self.json_request('/jobs/' + job_id + '/force', {})[0], 400)
        self.assertEqual(jobs.get(job_id)['status'], 'queued')
        self.assertFalse(jobs.get(job_id).get('forced'))
        jobs.action(job_id, 'stop')
        jobs.set_maintenance(False)
        self.assertEqual(jobs.get(job_id)['status'], 'stopped')

    def test_forced_retries_stay_parallel_and_recreation_returns_to_normal_queue(self):
        normal = self.submit(URL + '?case=hang')[1]['id']
        self.await_state(normal, {'running'})
        other = 'b'*40 + '-1234567890123'
        forced = self.submit(URL.replace(MEETING_ID,other) + '?case=transient')[1]['id']
        self.assertEqual(self.json_request('/jobs/' + forced + '/force', {})[0], 202)
        result = self.await_state(forced, {'done'}, timeout=12)
        self.assertEqual(result['attempt'], 2)
        self.assertTrue(result['forced'])
        self.assertEqual(self.server.jobs.get(normal)['status'], 'running')
        self.assertEqual(self.json_request('/jobs/' + forced + '/recreate', {})[0], 202)
        recreated = self.server.jobs.get(forced)
        self.assertEqual(recreated['status'], 'queued')
        self.assertFalse(recreated.get('forced'))
        self.server.jobs.action(forced, 'stop')
        self.server.jobs.action(normal, 'stop')
        self.await_state(normal, {'stopped'})
        self.await_state(forced, {'stopped'})

    def test_stop_forced_during_retry_delay(self):
        old_delay = web.RETRY_DELAY
        web.RETRY_DELAY = 2
        self.addCleanup(setattr, web, 'RETRY_DELAY', old_delay)
        normal = self.submit(URL + '?case=hang')[1]['id']
        self.await_state(normal, {'running'})
        other = 'b'*40 + '-1234567890123'
        forced = self.submit(URL.replace(MEETING_ID,other) + '?case=fail')[1]['id']
        self.assertEqual(self.json_request('/jobs/' + forced + '/force', {})[0], 202)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            job = self.server.jobs.get(forced)
            if job['status']=='queued' and job['attempt']==1:
                break
            time.sleep(.05)
        self.assertEqual(job['status'], 'queued')
        self.assertTrue(job['forced'])
        self.server.jobs.action(forced, 'stop')
        self.assertEqual(self.await_state(forced, {'stopped'})['attempt'], 1)
        self.assertEqual(self.server.jobs.get(normal)['status'], 'running')
        self.server.jobs.action(normal, 'stop')
        self.await_state(normal, {'stopped'})

    def test_forced_cap_is_atomic_under_concurrent_clicks(self):
        from concurrent.futures import ThreadPoolExecutor
        def url(letter):
            return URL.replace(MEETING_ID, letter*40 + '-1234567890123') + '?case=hang'
        ids = [self.submit(url(letter))[1]['id'] for letter in 'abcdef']
        self.await_state(ids[0], {'running'})
        with ThreadPoolExecutor(max_workers=5) as pool:
            codes = list(pool.map(lambda j: self.json_request('/jobs/' + j + '/force', {})[0], ids[1:]))
        self.assertEqual(codes.count(202), 2)
        self.assertEqual(codes.count(400), 3)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            jobs = self.json_request('/jobs')[1]
            if sum(j['status'] == 'running' for j in jobs) == 3:
                break
            time.sleep(.05)
        self.assertEqual(sum(j['status'] == 'running' for j in jobs), 3)
        for job_id in ids:
            status = self.json_request('/jobs/' + job_id)[1]['status']
            if status in ('running','queued'):
                self.assertEqual(self.json_request('/jobs/' + job_id + '/stop', {})[0], 202)
        for job_id in ids:
            self.await_state(job_id, {'stopped'})

    def test_unexpected_forced_worker_error_cleans_up_only_its_container(self):
        normal = self.submit(URL + '?case=hang')[1]['id']
        self.await_state(normal, {'running'})
        other = 'b'*40 + '-1234567890123'
        forced = self.submit(URL.replace(MEETING_ID,other) + '?case=hang')[1]['id']
        original = web.ffmpeg_progress
        triggered = threading.Event()
        def broken_progress(path):
            if ('.bbb-mp4-' + forced) in str(path) and (web.ROOT / ('bbb-mp4-' + forced + '.running')).exists() and not triggered.is_set():
                triggered.set()
                raise OSError('simulated unexpected read error')
            return original(path)
        web.ffmpeg_progress = broken_progress
        self.addCleanup(setattr, web, 'ffmpeg_progress', original)
        self.assertEqual(self.json_request('/jobs/' + forced + '/force', {})[0], 202)
        self.await_state(forced, {'failed'})
        self.assertTrue(triggered.is_set())
        self.assertEqual(self.server.jobs.get(normal)['status'], 'running')
        calls = (web.ROOT / 'calls').read_text()
        self.assertIn('rm -f bbb-mp4-' + forced, calls)
        self.assertNotIn('rm -f bbb-mp4-' + normal, calls)
        self.server.jobs.action(normal, 'stop')
        self.await_state(normal, {'stopped'})

    def test_forced_orphan_recovery_never_stops_other_job(self):
        old_timeout, original_remove = web.STALL_TIMEOUT, web.remove_container
        web.STALL_TIMEOUT = .3
        self.addCleanup(setattr, web, 'STALL_TIMEOUT', old_timeout)
        self.addCleanup(setattr, web, 'remove_container', original_remove)
        normal = self.submit(URL + '?case=hang')[1]['id']
        self.await_state(normal, {'running'})
        other = 'b'*40 + '-1234567890123'
        forced = self.submit(URL.replace(MEETING_ID,other) + '?case=stallprogress')[1]['id']
        allowed = threading.Event()
        def remove(name):
            if name == 'bbb-mp4-' + forced and not allowed.is_set():
                raise web.OrphanContainerError('simulated orphan')
            return original_remove(name)
        web.remove_container = remove
        self.assertEqual(self.json_request('/jobs/' + forced + '/force', {})[0], 202)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and not self.server.jobs.worker_error:
            time.sleep(.05)
        self.assertIsNotNone(self.server.jobs.worker_error)
        self.assertEqual(self.server.jobs.get(normal)['status'], 'running')
        self.assertNotIn('rm -f bbb-mp4-' + normal, (web.ROOT / 'calls').read_text())
        pending_id = self.submit(URL.replace(MEETING_ID, 'c'*40 + '-1234567890123'))[1]['id']
        self.assertEqual(self.json_request('/jobs/' + pending_id + '/force', {})[0], 400)
        self.assertEqual(self.server.jobs.get(pending_id)['status'], 'queued')
        allowed.set()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and self.server.jobs.worker_error:
            time.sleep(.05)
        self.assertIsNone(self.server.jobs.worker_error)
        self.assertEqual(self.server.jobs.get(normal)['status'], 'running')
        self.assertNotIn('rm -f bbb-mp4-' + normal, (web.ROOT / 'calls').read_text())
        self.server.jobs.action(pending_id, 'stop')
        for job_id in (normal, forced):
            status = self.server.jobs.get(job_id)['status']
            if status in ('queued','running'):
                self.server.jobs.action(job_id, 'stop')
        for job_id in (normal, forced, pending_id):
            self.await_state(job_id, {'stopped'})

    def test_forced_queue_survives_restart_and_cap(self):
        state = web.ROOT / 'forced-recovery'
        state.mkdir()
        jobs_data = []
        for index, letter in enumerate('abc'):
            meeting_id = letter*40 + '-1234567890123'
            jobs_data.append({'id':str(index+1)*32, 'meeting_id':meeting_id,
                              'url':URL.replace(MEETING_ID,meeting_id)+'?case=hang',
                              'status':'running', 'run_id':str(index+4)*32,
                              'created':time.time(), 'duration':None, 'capture_started':None,
                              'screenshot_time':None, 'error':None, 'forced':index>0})
        (state / 'jobs.json').write_text(json.dumps(jobs_data))
        jobs = web.Jobs(self.output, state)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and sum(j['status']=='running' for j in jobs.list()) < 3:
            time.sleep(.05)
        self.assertEqual(sum(j['status']=='running' for j in jobs.list()), 3)
        self.assertEqual(sum(bool(j.get('forced')) for j in jobs.list()), 2)
        for job in jobs.list(): jobs.action(job['id'], 'stop')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and any(j['status']!='stopped' for j in jobs.list()):
            time.sleep(.05)
        self.assertTrue(all(j['status']=='stopped' for j in jobs.list()))

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

    def test_settings_snapshot_and_actual_container_arguments(self):
        global_settings = {'resolution':'1920x1080', 'video_bitrate':3333, 'audio_bitrate':192, 'preset':'slow', 'codec':'h265', 'fps':50}
        self.assertEqual(self.json_request('/settings', global_settings)[0], 200)
        self.assertEqual(self.json_request('/settings', {**global_settings, 'video_bitrate':0})[0], 400)
        self.assertEqual(self.json_request('/settings')[1], global_settings)
        old_client = {k:v for k,v in global_settings.items() if k != 'fps'}
        self.assertEqual(self.json_request('/settings', old_client)[1]['fps'], 50)
        self.assertEqual(self.json_request('/settings', {**global_settings, 'resolution':'1280x720'})[0], 400)
        self.assertEqual(self.submit(URL, {'preset':'invalid'})[0], 400)
        self.assertEqual(self.submit(URL, {'resolution':'1280x720'})[0], 400)
        self.assertEqual(self.submit(URL, {'fps':61})[0], 400)
        self.assertEqual(self.submit(URL, {'fps':True})[0], 400)
        code, created = self.submit(URL, {'video_bitrate':'4000', 'fps':25})
        self.assertEqual(code, 202)
        job_id = created['id']
        expected = {**global_settings, 'video_bitrate':4000, 'fps':25}
        self.assertEqual(self.json_request('/jobs/' + job_id)[1]['settings'], expected)
        new_global = {**global_settings, 'video_bitrate':9000, 'resolution':'3440x1440', 'fps':60}
        self.assertEqual(self.json_request('/settings', new_global)[0], 200)
        job = self.await_state(job_id, {'done'})
        self.assertEqual(job['settings'], expected)
        self.assertEqual(job['verification']['duration'], 5.0)
        inspections = [json.loads(line) for line in (web.ROOT / 'inspect-args').read_text().splitlines()]
        self.assertEqual([x[x.index('--entrypoint')+1] for x in inspections], ['ffprobe', 'ffmpeg'])
        self.assertTrue(all(any(arg.endswith(':/probe:ro') for arg in x) for x in inspections))
        self.assertIn('-xerror', inspections[1])
        self.assertIn('0:v:0', inspections[1])
        self.assertIn('0:a:0', inspections[1])
        args = json.loads((web.ROOT / ('bbb-mp4-' + job_id + '.args')).read_text())
        for arg in ('BBB_MP4_SIZE=1920x1080','BBB_MP4_VIDEO_BITRATE=4000','BBB_MP4_AUDIO_BITRATE=192','BBB_MP4_PRESET=slow','BBB_MP4_CODEC=h265','BBB_MP4_FPS=25'):
            self.assertIn(arg, args)
        self.assertTrue(any(x.endswith('/ffmpeg-cmd.sh:/usr/src/app/ffmpeg-cmd.sh:ro') for x in args))
        self.assertEqual(self.json_request('/jobs/' + job_id + '/recreate', {})[0], 202)
        self.await_state(job_id, {'done'})
        self.assertEqual(self.json_request('/jobs/' + job_id)[1]['settings'], expected)
        self.assertEqual(web.Jobs(self.output, web.ROOT / 'state').get_settings(), new_global)

    def test_probe_rejects_missing_track_and_deletes_old_file(self):
        target = self.output / (MEETING_ID + '.mp4')
        target.write_bytes(b'previous good mp4')
        self.assertEqual(self.submit(URL + '?case=noaudio')[0], 409)
        self.assertEqual(target.read_bytes(), b'previous good mp4')
        job_id = self.submit(URL + '?case=noaudio', replace_confirmed=True)[1]['id']
        self.assertFalse(target.exists())
        job = self.await_state(job_id, {'failed'})
        self.assertIn('аудиодорожка', job['error'])
        self.assertFalse(target.exists())
        code, log = self.json_request('/jobs/' + job_id + '/log')
        self.assertEqual(code, 200)
        self.assertIn('Проверка MP4', log['log'])
        with self.server.jobs.condition:
            self.server.jobs.jobs[job_id]['url'] = URL + '?case=probeerror'
            self.server.jobs.save()
        self.assertEqual(self.json_request('/jobs/' + job_id + '/recreate', {})[0], 202)
        job = self.await_state(job_id, {'failed'})
        self.assertIn('invalid mp4', job['error'])
        self.assertFalse(target.exists())
        with self.server.jobs.condition:
            self.server.jobs.jobs[job_id]['url'] = URL + '?case=decodeerror'
            self.server.jobs.save()
        self.assertEqual(self.json_request('/jobs/' + job_id + '/recreate', {})[0], 202)
        job = self.await_state(job_id, {'failed'})
        self.assertIn('corrupt video frame', job['error'])
        self.assertFalse(target.exists())
        with self.server.jobs.condition:
            self.server.jobs.jobs[job_id]['url'] = URL + '?case=wrongsize'
            self.server.jobs.save()
        self.assertEqual(self.json_request('/jobs/' + job_id + '/recreate', {})[0], 202)
        job = self.await_state(job_id, {'failed'})
        self.assertIn('разрешение', job['error'])
        self.assertFalse(target.exists())

    def test_retry_transient_and_permanent_failure(self):
        job_id = self.submit(URL + '?case=transient')[1]['id']
        job = self.await_state(job_id, {'done'})
        self.assertEqual(job['attempt'], 2)
        self.assertIn('Повтор через', self.json_request('/jobs/' + job_id + '/log')[1]['log'])
        bad_id = 'c' * 40 + '-1234567890123'
        failed_id = self.submit(URL.replace(MEETING_ID, bad_id) + '?case=notfound')[1]['id']
        failed = self.await_state(failed_id, {'failed'})
        self.assertEqual(failed['attempt'], 1)

    def test_short_result_is_retried_and_not_published(self):
        job_id = self.submit(URL + '?case=short')[1]['id']
        job = self.await_state(job_id, {'failed'})
        self.assertEqual(job['attempt'], 3)
        self.assertIn('короче', job['error'])
        self.assertFalse((self.output / (MEETING_ID + '.mp4')).exists())

    def test_exhausted_retries_and_stop_while_waiting(self):
        old_delay = web.RETRY_DELAY
        web.RETRY_DELAY = 2
        try:
            job_id = self.submit(URL + '?case=fail')[1]['id']
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = self.json_request('/jobs/' + job_id)[1]
                if job['status'] == 'queued' and job.get('attempt') == 1:
                    break
                time.sleep(.05)
            self.assertEqual(job['status'], 'queued')
            self.assertEqual(self.json_request('/jobs/' + job_id + '/stop', {})[0], 202)
            self.assertEqual(self.await_state(job_id, {'stopped'})['attempt'], 1)
        finally:
            web.RETRY_DELAY = old_delay
        other = 'd' * 40 + '-1234567890123'
        failed_id = self.submit(URL.replace(MEETING_ID, other) + '?case=fail')[1]['id']
        self.assertEqual(self.await_state(failed_id, {'failed'})['attempt'], 3)

    def test_legacy_settings_and_metadata(self):
        state = web.ROOT / 'legacy'
        state.mkdir()
        legacy = {'resolution':'1280x720', 'video_bitrate':2500, 'audio_bitrate':128, 'preset':'fast'}
        (state / 'settings.json').write_text(json.dumps(legacy))
        jobs = web.Jobs(self.output, state)
        self.assertEqual(jobs.get_settings(), {**legacy, 'codec':'h264', 'fps':30})
        # Existing global resolution can be retained, but not newly selected.
        self.assertEqual(jobs.set_settings({**legacy, 'codec':'h264', 'fps':60})['resolution'], '1280x720')
        self.assertEqual(jobs.set_settings({**legacy, 'codec':'h264'})['fps'], 60)
        with self.assertRaises(ValueError):
            jobs.set_settings({**legacy, 'codec':'h264', 'resolution':'640x360'})
        job_id = jobs.submit(URL)
        self.assertEqual(jobs.get(job_id)['settings']['fps'], 60)
        self.assertEqual(jobs.get(job_id)['settings']['resolution'], '1280x720')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and jobs.get(job_id)['status'] != 'done':
            time.sleep(.05)
        self.assertEqual(jobs.get(job_id)['status'], 'done')
        args = json.loads((web.ROOT / ('bbb-mp4-' + job_id + '.args')).read_text())
        self.assertIn('BBB_MP4_SIZE=1280x720', args)
        self.assertIn('BBB_MP4_FPS=60', args)
        jobs.set_settings({**legacy, 'codec':'h264', 'resolution':'1280x800', 'fps':15})
        jobs.action(job_id, 'recreate')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and jobs.get(job_id)['status'] != 'done':
            time.sleep(.05)
        self.assertEqual(jobs.get(job_id)['status'], 'done')
        self.assertEqual(jobs.get(job_id)['settings']['resolution'], '1280x720')
        self.assertEqual(jobs.get(job_id)['settings']['fps'], 60)
        self.assertEqual(web.DEFAULT_SETTINGS['codec'], 'h264')
        self.assertIsNone(web.DEFAULT_SETTINGS['video_bitrate'])

    def test_old_job_snapshot_retains_resolution_and_30fps_after_update(self):
        state = web.ROOT / 'legacy-job'
        state.mkdir()
        job_id = '1'*32
        legacy_settings = {'resolution':'640x360', 'video_bitrate':2500,
                           'audio_bitrate':128, 'preset':'fast', 'codec':'h264'}
        (state / 'jobs.json').write_text(json.dumps([{
            'id':job_id, 'meeting_id':MEETING_ID, 'url':URL, 'status':'stopped',
            'created':time.time(), 'run_id':None, 'settings':legacy_settings}]))
        jobs = web.Jobs(self.output, state)
        self.assertEqual(jobs.get(job_id)['settings'], {**legacy_settings, 'fps':30})
        jobs.set_settings({**web.DEFAULT_SETTINGS, 'fps':60, 'resolution':'3440x1440'})
        jobs.action(job_id, 'recreate')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and jobs.get(job_id)['status'] != 'done':
            time.sleep(.05)
        self.assertEqual(jobs.get(job_id)['status'], 'done')
        args = json.loads((web.ROOT / ('bbb-mp4-' + job_id + '.args')).read_text())
        self.assertIn('BBB_MP4_SIZE=640x360', args)
        self.assertIn('BBB_MP4_FPS=30', args)
        self.assertEqual(jobs.get(job_id)['settings']['fps'], 30)

    def test_supported_resolution_and_frame_rate_values(self):
        for size in ('1280x800','1920x1080','2560x1440','2560x1080','3440x1440'):
            self.assertEqual(web.validate_settings({'resolution':size})['resolution'], size)
        for fps in (15,24,25,30,50,60):
            self.assertEqual(web.validate_settings({'fps':str(fps)})['fps'], fps)
        for low in ('640x360','854x480','1280x720'):
            with self.assertRaises(ValueError): web.validate_settings({'resolution':low})
        for fps in (True, 0, 29, 61, '1.5'):
            with self.assertRaises(ValueError): web.validate_settings({'fps':fps})

    def test_low_space_is_reported_and_recorded(self):
        old = web.LOW_SPACE_BYTES
        web.LOW_SPACE_BYTES = 2**100
        self.addCleanup(setattr, web, 'LOW_SPACE_BYTES', old)
        self.assertTrue(self.json_request('/disk')[1]['low'])
        job_id = self.submit(URL)[1]['id']
        self.assertTrue(self.await_state(job_id, {'running'})['space_warning'])
        self.await_state(job_id, {'done'})
        self.assertIn('меньше 1 ГиБ', self.json_request('/jobs/' + job_id + '/log')[1]['log'])

    def test_stop_during_verification(self):
        job_id = self.submit(URL + '?case=slowprobe')[1]['id']
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            job = self.json_request('/jobs/' + job_id)[1]
            if job.get('phase') == 'verifying':
                break
            time.sleep(.05)
        self.assertEqual(job.get('phase'), 'verifying')
        self.assertEqual(self.json_request('/jobs/' + job_id + '/stop', {})[0], 202)
        self.assertEqual(self.await_state(job_id, {'stopped'})['status'], 'stopped')
        self.assertFalse((self.output / (MEETING_ID + '.mp4')).exists())
        self.assertIn('stop --time 2 bbb-probe-' + job_id, (web.ROOT / 'calls').read_text())

    def test_duration_override_and_diagnostic_warnings(self):
        self.assertEqual(web.parse_duration('3:04:25'), 11065)
        for invalid in ('-3:04:25', '3:99:25', 'nan', '-1', '604801'):
            with self.assertRaises(ValueError):
                web.parse_duration(invalid)
        code, created = self.json_request('/jobs', {'url': URL + '?case=warnings', 'duration_override': '3:04:25'})
        self.assertEqual(code, 202)
        job_id = created['id']
        job = self.await_state(job_id, {'done'})
        self.assertEqual(job['duration_override'], 11065)
        self.assertEqual(len(job['verification']['warnings']), 2)
        args = json.loads((web.ROOT / ('bbb-mp4-' + job_id + '.args')).read_text())
        self.assertIn('BBB_MP4_DURATION=11065', args)
        code, _ = self.json_request('/jobs/' + job_id + '/recreate', {'duration_override': '1:02:03'})
        self.assertEqual(code, 202)
        job = self.await_state(job_id, {'done'})
        self.assertEqual(job['duration_override'], 3723)

    def test_failed_state_write_does_not_delete_existing_result(self):
        job_id = self.submit(URL)[1]['id']
        self.await_state(job_id, {'done'})
        target = self.output / (MEETING_ID + '.mp4')
        original = target.read_bytes()
        with mock.patch.object(self.server.jobs, 'save', side_effect=OSError('disk full')):
            self.assertEqual(self.json_request('/jobs/' + job_id + '/recreate', {})[0], 500)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(self.server.jobs.get(job_id)['status'], 'done')
            self.assertEqual(self.json_request('/jobs/' + job_id + '/delete', {})[0], 500)
            self.assertEqual(target.read_bytes(), original)

    def test_unfinished_delete_cannot_be_recreated(self):
        job_id = self.submit(URL)[1]['id']
        self.await_state(job_id, {'done'})
        with mock.patch.object(self.server.jobs, 'remove_result', side_effect=OSError('permission denied')):
            self.assertEqual(self.json_request('/jobs/' + job_id + '/delete', {})[0], 500)
        self.assertIn('Удаление не завершено', self.server.jobs.get(job_id)['error'])
        self.assertEqual(self.json_request('/jobs/' + job_id + '/recreate', {})[0], 400)
        self.assertEqual(self.json_request('/jobs/' + job_id + '/delete', {})[0], 202)
        self.assertFalse((self.output / (MEETING_ID + '.mp4')).exists())

    def test_worker_survives_state_write_failure(self):
        jobs = self.server.jobs
        original = jobs.save
        def fail():
            raise OSError('simulated full disk')
        # Keep the worker out until submit has durably queued the job.
        with jobs.condition:
            job_id = jobs.submit(URL)
            jobs.save = fail
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not jobs.persistence_error:
            time.sleep(.02)
        self.assertIn('simulated full disk', jobs.persistence_error)
        self.assertEqual(jobs.get(job_id)['status'], 'running')
        self.assertFalse((web.ROOT / ('bbb-mp4-' + job_id + '.args')).exists())
        jobs.save = original
        with jobs.condition:
            jobs.condition.notify_all()
        self.assertEqual(self.await_state(job_id, {'done'})['status'], 'done')
        self.assertIsNone(jobs.persistence_error)

    def test_hung_start_is_stopped_and_retried(self):
        old = web.PREPARE_TIMEOUT
        web.PREPARE_TIMEOUT = .3
        self.addCleanup(setattr, web, 'PREPARE_TIMEOUT', old)
        job_id = self.submit(URL + '?case=hang')[1]['id']
        job = self.await_state(job_id, {'failed'}, timeout=12)
        self.assertIn('Превышено допустимое время', job['error'])
        self.assertEqual(job['attempt'], 3)
        calls = (web.ROOT / 'calls').read_text()
        self.assertIn('rm -f bbb-mp4-' + job_id, calls)

    def test_stalled_ffmpeg_progress_is_stopped_and_retried(self):
        old = web.STALL_TIMEOUT
        web.STALL_TIMEOUT = .3
        self.addCleanup(setattr, web, 'STALL_TIMEOUT', old)
        job_id = self.submit(URL + '?case=stallprogress')[1]['id']
        job = self.await_state(job_id, {'failed'}, timeout=12)
        self.assertIn('Нет прогресса FFmpeg', job['error'])
        self.assertEqual(job['attempt'], 3)

    def test_slow_ffmpeg_frames_fail_early_without_blind_retries(self):
        warmup, lag = web.CAPTURE_LAG_WARMUP, web.MAX_CAPTURE_LAG
        web.CAPTURE_LAG_WARMUP, web.MAX_CAPTURE_LAG = .8, .25
        self.addCleanup(setattr, web, 'CAPTURE_LAG_WARMUP', warmup)
        self.addCleanup(setattr, web, 'MAX_CAPTURE_LAG', lag)
        job_id = self.submit(URL + '?case=slowprogress')[1]['id']
        job = self.await_state(job_id, {'failed'}, timeout=8)
        self.assertIn('Кодирование не успевает', job['error'])
        self.assertEqual(job['attempt'], 1)
        self.assertIn('rm -f bbb-mp4-' + job_id, (web.ROOT / 'calls').read_text())

    def test_completed_frames_are_allowed_to_finish_encoding(self):
        warmup, lag = web.CAPTURE_LAG_WARMUP, web.MAX_CAPTURE_LAG
        web.CAPTURE_LAG_WARMUP, web.MAX_CAPTURE_LAG = .1, .1
        self.addCleanup(setattr, web, 'CAPTURE_LAG_WARMUP', warmup)
        self.addCleanup(setattr, web, 'MAX_CAPTURE_LAG', lag)
        job_id = self.submit(URL + '?case=flushprogress')[1]['id']
        job = self.await_state(job_id, {'done', 'failed'}, timeout=8)
        self.assertEqual(job['status'], 'done', job.get('error'))

    def test_progress_uses_frames_not_elapsed_time(self):
        job_id = self.submit(URL + '?case=stallprogress')[1]['id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.server.jobs.get(job_id)
            if job['progress'] is not None:
                break
            time.sleep(.05)
        self.assertEqual(job['progress'], 0)
        time.sleep(.6)
        self.assertEqual(self.server.jobs.get(job_id)['progress'], 0)
        self.server.jobs.action(job_id, 'stop')
        self.await_state(job_id, {'stopped'})

    def test_queue_waits_if_container_removal_cannot_be_confirmed(self):
        old_timeout, original_remove = web.PREPARE_TIMEOUT, web.remove_container
        web.PREPARE_TIMEOUT = .3
        allowed = threading.Event()
        def remove(name):
            if not allowed.is_set():
                raise web.OrphanContainerError('simulated Docker removal failure')
            original_remove(name)
        web.remove_container = remove
        def cleanup():
            allowed.set()
            web.remove_container = original_remove
            web.PREPARE_TIMEOUT = old_timeout
        self.addCleanup(cleanup)
        job_id = self.submit(URL + '?case=hang')[1]['id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.server.jobs.worker_error:
            time.sleep(.05)
        self.assertIsNotNone(self.server.jobs.worker_error)
        other = 'd'*40 + '-1234567890123'
        other_id = self.submit(URL.replace(MEETING_ID, other))[1]['id']
        time.sleep(.3)
        self.assertEqual(self.server.jobs.get(other_id)['status'], 'queued')
        self.assertFalse((web.ROOT / ('bbb-mp4-' + other_id + '.args')).exists())
        allowed.set()
        # Recovery sleeps up to five seconds after a failed cleanup.
        self.assertEqual(self.await_state(other_id, {'done'}, timeout=12)['status'], 'done')
        self.server.jobs.action(job_id, 'stop')
        self.await_state(job_id, {'stopped'})

    def test_maintenance_prevents_restart_during_active_work(self):
        self.assertEqual(self.json_request('/maintenance', {'enabled':True})[0], 200)
        self.assertEqual(self.submit(URL)[0], 400)
        self.assertEqual(self.json_request('/maintenance', {'enabled':False})[0], 200)
        job_id = self.submit(URL)[1]['id']
        self.await_state(job_id, {'running'})
        self.assertEqual(self.json_request('/maintenance', {'enabled':True})[0], 400)
        self.await_state(job_id, {'done'})
        self.assertEqual(self.json_request('/maintenance', {'enabled':True})[0], 200)
        self.assertTrue(self.json_request('/health')[1]['maintenance'])
        self.json_request('/maintenance', {'enabled':False})

    def test_recover_already_published_file_without_recapture(self):
        state = web.ROOT / 'finished-recovery'
        state.mkdir()
        job_id, run_id = '7'*32, '8'*32
        target = self.output / (MEETING_ID + '.mp4')
        target.write_bytes(b'test mp4')
        (web.ROOT / 'received').write_text(URL)
        (web.ROOT / ('bbb-mp4-' + job_id + '.args')).write_text(json.dumps(['BBB_MP4_SIZE=1280x800']))
        (state / 'jobs.json').write_text(json.dumps([{'id':job_id,'meeting_id':MEETING_ID,'url':URL,
            'status':'running','phase':'verifying','run_id':run_id,'created':time.time(),
            'duration':5,'capture_started':time.time(),'screenshot_time':None,'error':None}]))
        jobs = web.Jobs(self.output, state)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and jobs.get(job_id)['status'] != 'done':
            time.sleep(.05)
        self.assertEqual(jobs.get(job_id)['status'], 'done')
        self.assertEqual(target.read_bytes(), b'test mp4')
        calls = (web.ROOT / 'calls').read_text()
        self.assertNotIn('run --rm --name bbb-mp4-' + job_id, calls)

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
             'status':'queued','run_id':'4'*32,'cleanup_run_id':'5'*32,'created':time.time(), 'queued_at':time.time_ns(),
             'duration':None, 'capture_started':None, 'screenshot_time':None, 'error':None}]))
        workspace = self.output / ('.bbb-mp4-' + job_id + '-' + '2'*32)
        workspace.mkdir()
        (workspace / 'partial.mp4').write_bytes(b'incomplete')
        old_workspace = self.output / ('.bbb-mp4-' + queued_id + '-' + '5'*32)
        old_workspace.mkdir()
        jobs = web.Jobs(self.output, state)
        self.assertFalse(old_workspace.exists())
        self.assertFalse(workspace.exists())
        self.assertIn(jobs.get(job_id)['status'], ('queued', 'running', 'done'))
        self.assertTrue((web.ROOT / ('bbb-mp4-' + job_id + '.stop')).exists())
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and jobs.get(queued_id)['status'] != 'done':
            time.sleep(.05)
        self.assertEqual(jobs.get(queued_id)['status'], 'done')
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and jobs.get(job_id)['status'] != 'done':
            time.sleep(.05)
        self.assertEqual(jobs.get(job_id)['status'], 'done')


if __name__ == '__main__':
    unittest.main()
