import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parent.parent


class MaintenanceHandler(http.server.BaseHTTPRequestHandler):
    active = False
    calls = []
    def log_message(self, *args): pass
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.calls.append(data['enabled'])
        self.send_response(400 if self.active and data['enabled'] else 200)
        self.end_headers()
        self.wfile.write(b'{}')


class DeploymentTest(unittest.TestCase):
    def test_refuses_to_restart_active_job_and_releases_maintenance(self):
        try:
            server = http.server.HTTPServer(('127.0.0.1', 0), MaintenanceHandler)
        except OSError:
            self.skipTest('port 8080 is already in use')
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); repo = root / 'repo'; repo.mkdir()
            for name in ('deploy.sh', 'convert.sh', 'web.py', 'web.html', 'ffmpeg-cmd.sh', 'bbb-mp4.js', 'operations.py'):
                shutil.copy(ROOT / name, repo)
            deploy = repo / 'deploy.sh'
            deploy.write_text(deploy.read_text().replace('127.0.0.1:8080', '127.0.0.1:' + str(server.server_port)))
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            bin_dir = root / 'bin'; bin_dir.mkdir()
            mocks = {
                'docker': "if [ \"$1\" = image ]; then printf 'manishkatyan/bbb-mp4@sha256:%064d\\n' 0; fi",
                'sudo': 'exec "$@"',
                'install': 'cp "$3" "$UNIT_COPY"',
                'systemctl': 'echo "$*" >> "$SYSTEMCTL_LOG"',
            }
            for name, body in mocks.items():
                p = bin_dir / name; p.write_text('#!/bin/sh\n' + body + '\n'); p.chmod(0o755)
            env = {**os.environ, 'PATH': str(bin_dir) + ':' + os.environ['PATH'],
                   'UNIT_COPY': str(root / 'unit'), 'SYSTEMCTL_LOG': str(root / 'systemctl')}
            MaintenanceHandler.calls = []; MaintenanceHandler.active = True
            result = subprocess.run(['bash', str(repo / 'deploy.sh')], env=env, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'systemctl').exists())
            self.assertEqual(MaintenanceHandler.calls, [True])
            MaintenanceHandler.calls = []; MaintenanceHandler.active = False
            result = subprocess.run(['bash', str(repo / 'deploy.sh')], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(MaintenanceHandler.calls, [True, False])
            self.assertIn('restart bbb-mp4-web.service', (root / 'systemctl').read_text())


if __name__ == '__main__': unittest.main()
