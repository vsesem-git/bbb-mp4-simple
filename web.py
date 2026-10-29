#!/usr/bin/env python3
"""Small trusted-network frontend for the existing one-off converter."""
import json
import ipaddress
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
URL_RE = re.compile(r'^https?://[^/?#\s]+/playback/presentation/2\.3/([a-z0-9]{40}-[0-9]{13})(?:[?#][^\s]*)?$')
FILE_RE = re.compile(r'^[a-z0-9]{40}-[0-9]{13}\.mp4$')
JOB_RE = re.compile(r'^[a-f0-9]{32}$')
TRUSTED_NETWORKS = tuple(ipaddress.ip_network(net) for net in
                         ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def output_directory():
    # The converter uses the same local shell configuration. Do not evaluate
    # shell syntax supplied in a request; only the trusted .env is sourced.
    result = subprocess.run(
        ['bash', '-c', 'set -eu; source "$1"; printf "%s" "$COPY_TO_LOCATION"',
         'bash', str(ROOT / '.env')], check=True, capture_output=True, text=True)
    path = result.stdout
    if not path.startswith('/') or ':' in path:
        raise ValueError('COPY_TO_LOCATION must be an absolute path without a colon')
    return Path(path)


def signature(path):
    try:
        stat = path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    except FileNotFoundError:
        return None


class Jobs:
    def __init__(self, output):
        self.output = output
        self.lock = threading.Lock()
        self.jobs = {}
        self.active = set()
        self.queue = queue.Queue(maxsize=100)
        threading.Thread(target=self.worker, daemon=True).start()

    def submit(self, url):
        match = URL_RE.fullmatch(url)
        if not match:
            raise ValueError('Неверный playback URL BigBlueButton 2.3.')
        meeting_id = match.group(1)
        with self.lock:
            if meeting_id in self.active:
                raise ValueError('Для этой записи задание уже выполняется или ожидает запуска.')
            job_id = secrets.token_hex(16)
            job = {'status': 'queued', 'meeting_id': meeting_id}
            self.jobs[job_id] = job
            self.active.add(meeting_id)
            try:
                self.queue.put_nowait((job_id, url))
            except queue.Full:
                del self.jobs[job_id]
                self.active.remove(meeting_id)
                raise ValueError('Очередь заполнена. Попробуйте позже.')
        return job_id

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    def worker(self):
        while True:
            job_id, url = self.queue.get()
            with self.lock:
                job = self.jobs[job_id]
                job['status'] = 'running'
                meeting_id = job['meeting_id']
            target = self.output / (meeting_id + '.mp4')
            try:
                before = signature(target)
                # stdout/stderr go to the service journal, not into the web page.
                result = subprocess.run([str(ROOT / 'convert.sh'), url], cwd=ROOT)
                after = signature(target)
                if result.returncode != 0 or after is None or after == before or after[2] == 0:
                    raise RuntimeError('Конвертация не создала новый MP4. Проверьте журнал сервиса.')
                with self.lock:
                    job['status'] = 'done'
                    job['download'] = '/download/' + target.name
            except Exception as exc:
                print('Conversion failed:', exc, flush=True)
                with self.lock:
                    job['status'] = 'failed'
                    job['error'] = 'Не удалось создать MP4. Проверьте журнал сервиса.'
            finally:
                with self.lock:
                    self.active.discard(meeting_id)
                self.queue.task_done()


class Handler(BaseHTTPRequestHandler):
    def respond_json(self, code, value):
        data = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != '/jobs':
            self.send_error(404)
            return
        if self.headers.get_content_type() != 'application/json':
            self.send_error(415)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192:
                raise ValueError('Некорректный размер запроса.')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or not isinstance(payload.get('url'), str):
                raise ValueError('Передайте URL записи.')
            job_id = self.server.jobs.submit(payload['url'])
        except (ValueError, UnicodeDecodeError) as exc:
            self.respond_json(400, {'error': str(exc)})
            return
        self.respond_json(202, {'id': job_id})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/':
            data = (ROOT / 'web.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)
        elif path.startswith('/jobs/') and JOB_RE.fullmatch(path[len('/jobs/'):]):
            job = self.server.jobs.get(path[len('/jobs/'):])
            self.respond_json(200, job) if job else self.respond_json(404, {'error': 'Задание не найдено.'})
        elif path.startswith('/download/') and FILE_RE.fullmatch(path[len('/download/'):]):
            name = path[len('/download/'):]
            try:
                file = (self.server.jobs.output / name).open('rb')
            except FileNotFoundError:
                self.send_error(404)
                return
            with file:
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Disposition', f'attachment; filename="{name}"')
                self.send_header('Content-Length', str(os.fstat(file.fileno()).st_size))
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                try:
                    shutil.copyfileobj(file, self.wfile)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        else:
            self.send_error(404)


class TrustedHTTPServer(ThreadingHTTPServer):
    def verify_request(self, request, client_address):
        address = ipaddress.ip_address(client_address[0])
        return any(address in network for network in TRUSTED_NETWORKS)


def main():
    output = output_directory()
    output.mkdir(parents=True, exist_ok=True)
    server = TrustedHTTPServer(('0.0.0.0', 8080), Handler)
    server.jobs = Jobs(output)
    print('Веб-интерфейс: http://0.0.0.0:8080', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
