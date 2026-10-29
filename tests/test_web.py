import importlib.util
import json
import os
from pathlib import Path
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
        (root / 'web.html').write_text('web page')
        (root / '.env').write_text("COPY_TO_LOCATION='" + str(root / 'results with spaces') + "'\n")
        self.output = web.output_directory()
        self.output.mkdir()
        converter = root / 'convert.sh'
        converter.write_text('''#!/bin/bash
set -eu
source "$(dirname "$0")/.env"
printf '%s' "$1" > "$(dirname "$0")/received"
sleep 0.3
case "$1" in *case=fail*) exit 1;; *case=stale*) exit 0;; esac
id=${1##*/}; id=${id%%\\?*}; id=${id%%\\#*}
printf 'test mp4' > "$COPY_TO_LOCATION/$id.mp4"
''')
        converter.chmod(0o755)
        self.server = web.TrustedHTTPServer(('127.0.0.1', 0), web.Handler)
        self.server.jobs = web.Jobs(self.output)
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

    def job(self, url):
        code, data = self.request('/jobs', {'url': url})
        return code, json.loads(data)

    def await_job(self, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, data = self.request('/jobs/' + job_id)
            self.assertEqual(code, 200)
            job = json.loads(data)
            if job['status'] in ('done', 'failed'):
                return job
            time.sleep(.05)
        self.fail('job timed out')

    def test_jobs_run_one_at_a_time(self):
        first_id = self.job(URL)[1]['id']
        other_url = URL.replace(MEETING_ID, 'a' * 40 + '-1234567890123')
        code, second = self.job(other_url)
        self.assertEqual(code, 202)
        code, data = self.request('/jobs/' + second['id'])
        self.assertEqual(json.loads(data)['status'], 'queued')
        self.assertEqual(self.await_job(first_id)['status'], 'done')
        self.assertEqual(self.await_job(second['id'])['status'], 'done')

    def test_submission_completion_download_and_failures(self):
        self.assertEqual(self.request('/')[0], 200)
        self.assertTrue(self.server.verify_request(None, ('192.168.1.2', 10)))
        self.assertFalse(self.server.verify_request(None, ('8.8.8.8', 10)))
        self.assertEqual(self.job('https://example.org/../../bad')[0], 400)
        url = URL + '?token=a&x=b#test'
        code, submitted = self.job(url)
        self.assertEqual(code, 202)
        self.assertEqual(self.job(url)[0], 400)  # duplicate in progress
        job = self.await_job(submitted['id'])
        self.assertEqual(job['status'], 'done')
        self.assertEqual((web.ROOT / 'received').read_text(), url)
        self.assertEqual(job['download'], '/download/' + MEETING_ID + '.mp4')
        self.assertEqual(self.request(job['download']), (200, b'test mp4'))
        self.assertEqual(self.request('/download/../.env')[0], 404)
        # Existing output must not make a failed or no-output job look successful.
        for suffix in ('?case=fail', '?case=stale'):
            code, submitted = self.job(URL + suffix)
            self.assertEqual(code, 202)
            self.assertEqual(self.await_job(submitted['id'])['status'], 'failed')
        self.assertEqual(self.request('/jobs/' + 'a' * 32)[0], 404)
        # Download links remain valid without in-memory job state.
        self.server.jobs = web.Jobs(self.output)
        self.assertEqual(self.request(job['download']), (200, b'test mp4'))


if __name__ == '__main__':
    unittest.main()
