#!/usr/bin/env python3
"""Trusted-network interface around the original one-off converter."""
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
URL_RE = re.compile(r'^https?://[^/?#\s]+/playback/presentation/2\.3/([a-z0-9]{40}-[0-9]{13})(?:[?#][^\s]*)?$')
FILE_RE = re.compile(r'^[a-z0-9]{40}-[0-9]{13}\.mp4$')
JOB_RE = re.compile(r'^[a-f0-9]{32}$')
TRUSTED_NETWORKS = tuple(ipaddress.ip_network(net) for net in
                         ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
ACTIVE = {'queued', 'running', 'stopping'}
# Capture the existing Xvfb display, without modifying the original image.
CAPTURE = r'''for x in /tmp/.X11-unix/X[0-9]*; do
  [ -S "$x" ] || continue
  display=${x##*X}
  exec ffmpeg -v error -f x11grab -video_size 1280x800 -i ":$display" -frames:v 1 -f image2pipe -vcodec mjpeg pipe:1
done
exit 1'''


def output_directory():
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
    def __init__(self, output, state_dir=None, screenshot_interval=60, retry_interval=5):
        self.output = Path(output)
        self.state_dir = Path(state_dir or ROOT / 'state')
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.state_file = self.state_dir / 'jobs.json'
        self.condition = threading.Condition()
        self.screenshot_interval = screenshot_interval
        self.retry_interval = retry_interval
        self.jobs = {}
        if self.state_file.exists():
            # Fail closed on corrupt state, rather than lose jobs or overwrite files.
            data = json.loads(self.state_file.read_text(encoding='utf-8'))
            for job in data:
                if not JOB_RE.fullmatch(job['id']) or not FILE_RE.fullmatch(job['meeting_id'] + '.mp4'):
                    raise ValueError('Некорректный файл состояния заданий')
                if job.get('run_id') is not None and not JOB_RE.fullmatch(job['run_id']):
                    raise ValueError('Некорректный идентификатор запуска')
                self.jobs[job['id']] = job
        self.recover()
        self.import_files()
        with self.condition:
            self.save()
        threading.Thread(target=self.worker, daemon=True).start()

    def save(self):
        """Call with condition held; state includes URLs and is private to the service user."""
        tmp = self.state_dir / 'jobs.json.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(list(self.jobs.values()), f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_file)

    def recover(self):
        # A restarted docker client may have left a container running. Do not
        # start another job until it is removed, or claim it has stopped.
        for job in self.jobs.values():
            if job['status'] in ('running', 'stopping'):
                name = 'bbb-mp4-' + job['id']
                result = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, text=True, timeout=30)
                if result.returncode and 'No such container' not in result.stderr:
                    raise RuntimeError('Не удалось завершить прежний контейнер: ' + result.stderr.strip())
                job.update(status='stopped', error='Прервано при перезапуске сервиса.', duration=None, capture_started=None)
            if job.get('run_id'):
                self.remove_workspace(job['id'], job['run_id'])

    def workspace(self, job_id, run_id):
        return self.output / ('.bbb-mp4-' + job_id + '-' + run_id)

    def remove_workspace(self, job_id, run_id):
        path = self.workspace(job_id, run_id)
        if path.exists():
            shutil.rmtree(path)

    def import_files(self):
        # Older versions had no persistent job list. Expose their existing
        # outputs without fabricating a playback URL.
        known = {j['meeting_id'] for j in self.jobs.values()}
        for path in sorted(self.output.iterdir()):
            if FILE_RE.fullmatch(path.name) and path.is_file() and path.stem not in known:
                job_id = secrets.token_hex(16)
                self.jobs[job_id] = {'id': job_id, 'meeting_id': path.stem, 'url': None,
                                     'status': 'done', 'created': time.time(), 'run_id': None,
                                     'duration': None, 'capture_started': None, 'screenshot_time': None,
                                     'error': None}

    def snapshot(self, job):
        item = dict(job)
        item.pop('run_id', None)
        target = self.output / (job['meeting_id'] + '.mp4')
        item['has_file'] = target.is_file()
        item['download'] = '/download/' + target.name if item['has_file'] else None
        if job['status'] == 'done' and not item['has_file']:
            item['error'] = 'MP4-файл не найден на диске.'
        item['preview'] = '/preview/' + job['id'] + '.jpg' if job.get('screenshot_time') else None
        item['progress'] = item['remaining'] = None
        if job['status'] == 'done':
            item['progress'], item['remaining'] = 100, 0
        elif job['status'] in ('running', 'stopping') and job.get('duration') and job.get('capture_started'):
            elapsed = max(0, time.time() - job['capture_started'])
            item['progress'] = min(99, max(0, round(100 * elapsed / job['duration'])))
            item['remaining'] = max(0, math.ceil(job['duration'] - elapsed))
        return item

    def list(self):
        with self.condition:
            return [self.snapshot(j) for j in self.jobs.values()]

    def get(self, job_id):
        with self.condition:
            job = self.jobs.get(job_id)
            return self.snapshot(job) if job else None

    def enqueue(self, job):
        if sum(j.get('status') == 'queued' for j in self.jobs.values()) >= 100:
            raise ValueError('Очередь заполнена. Попробуйте позже.')
        job.update(status='queued', queued_at=time.time_ns(), run_id=secrets.token_hex(16), duration=None,
                   capture_started=None, screenshot_time=None, error=None)
        (self.state_dir / (job['id'] + '.jpg')).unlink(missing_ok=True)
        self.save()
        self.condition.notify()

    def submit(self, url):
        match = URL_RE.fullmatch(url)
        if not match:
            raise ValueError('Неверный playback URL BigBlueButton 2.3.')
        meeting_id = match.group(1)
        with self.condition:
            existing = next((j for j in self.jobs.values() if j['meeting_id'] == meeting_id), None)
            if existing:
                if existing['url'] is None and existing['status'] not in ACTIVE:
                    existing['url'] = url
                    self.enqueue(existing)
                    return existing['id']
                raise ValueError('Эта запись уже есть в списке заданий.')
            job_id = secrets.token_hex(16)
            job = {'id': job_id, 'meeting_id': meeting_id, 'url': url, 'created': time.time()}
            self.jobs[job_id] = job
            try:
                self.enqueue(job)
            except Exception:
                del self.jobs[job_id]
                raise
            return job_id

    def action(self, job_id, action):
        with self.condition:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            if action == 'stop':
                if job['status'] == 'queued':
                    job.update(status='stopped', error=None)
                elif job['status'] == 'running':
                    job['status'] = 'stopping'
                else:
                    raise ValueError('Это задание сейчас нельзя остановить.')
                self.save()
            elif action == 'recreate':
                if job['status'] in ACTIVE:
                    raise ValueError('Дождитесь завершения или остановите задание.')
                if not job.get('url'):
                    raise ValueError('Для старого файла неизвестен URL. Вставьте его в форму выше.')
                self.enqueue(job)
            elif action == 'delete':
                if job['status'] in ACTIVE:
                    raise ValueError('Сначала остановите задание.')
                # Delete the recording only on an explicit delete action.
                (self.output / (job['meeting_id'] + '.mp4')).unlink(missing_ok=True)
                (self.state_dir / (job_id + '.jpg')).unlink(missing_ok=True)
                del self.jobs[job_id]
                self.save()
            else:
                raise ValueError('Неизвестное действие.')

    def log_reader(self, process, job_id, run_id):
        for line in process.stdout:
            print(line, end='', flush=True)
            value = line.strip()
            with self.condition:
                job = self.jobs.get(job_id)
                if not job or job.get('run_id') != run_id or job['status'] not in ('running', 'stopping'):
                    continue
                changed = False
                if re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value):
                    duration = float(value)
                    if math.isfinite(duration) and duration > 0 and job.get('duration') is None:
                        job['duration'] = duration
                        changed = True
                if 'Output #0, mp4, to ' in line and job.get('capture_started') is None:
                    job['capture_started'] = time.time()
                    changed = True
                if changed:
                    try:
                        self.save()
                    except OSError as exc:
                        print('Не удалось сохранить прогресс:', exc, flush=True)

    def screenshots(self, job_id, run_id, finished):
        name = 'bbb-mp4-' + job_id
        next_warning = time.monotonic() + 30
        while not finished.is_set():
            try:
                result = subprocess.run(['docker', 'exec', name, 'sh', '-c', CAPTURE],
                                        capture_output=True, timeout=15)
                image = result.stdout
                if result.returncode or not image.startswith(b'\xff\xd8') or len(image) > 5_000_000:
                    detail = result.stderr.decode('utf-8', errors='replace')[:300].strip()
                    raise ValueError(detail or 'Скриншот ещё недоступен')
                with self.condition:
                    job = self.jobs.get(job_id)
                    if not job or job.get('run_id') != run_id or job['status'] != 'running':
                        return
                    tmp = self.state_dir / (job_id + '.jpg.tmp')
                    tmp.write_bytes(image)
                    os.replace(tmp, self.state_dir / (job_id + '.jpg'))
                    job['screenshot_time'] = time.time_ns()
                    self.save()
                delay = self.screenshot_interval
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                if time.monotonic() >= next_warning:
                    print('Не удалось получить скриншот:', exc, flush=True)
                    next_warning = time.monotonic() + 60
                delay = self.retry_interval
            finished.wait(delay)

    def worker(self):
        while True:
            with self.condition:
                job = min((j for j in self.jobs.values() if j['status'] == 'queued'), key=lambda j: j.get('queued_at', 0), default=None)
                if not job:
                    self.condition.wait()
                    continue
                job['status'] = 'running'
                self.save()
                job_id, run_id, url = job['id'], job['run_id'], job['url']
                target = self.output / (job['meeting_id'] + '.mp4')
            finished = threading.Event()
            workspace = self.workspace(job_id, run_id)
            try:
                self.remove_workspace(job_id, run_id)
                workspace.mkdir(mode=0o700)
                staged = workspace / target.name
                env = os.environ.copy()
                env['BBB_MP4_OUTPUT_LOCATION'] = str(workspace)
                env['BBB_MP4_CONTAINER_NAME'] = 'bbb-mp4-' + job_id
                process = subprocess.Popen([str(ROOT / 'convert.sh'), url], cwd=ROOT, env=env,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, errors='replace', bufsize=1)
                reader = threading.Thread(target=self.log_reader, args=(process, job_id, run_id), daemon=True)
                reader.start()
                threading.Thread(target=self.screenshots, args=(job_id, run_id, finished), daemon=True).start()
                stop_succeeded = False
                while process.poll() is None:
                    with self.condition:
                        stopping = self.jobs[job_id]['status'] == 'stopping'
                    if stopping:
                        # Retry if requested before docker has created the named container.
                        try:
                            stop = subprocess.run(['docker', 'stop', '--time', '5', 'bbb-mp4-' + job_id],
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                            stop_succeeded = stop_succeeded or stop.returncode == 0
                        except (OSError, subprocess.SubprocessError) as exc:
                            print('Container stop failed:', exc, flush=True)
                    time.sleep(.5)
                reader.join()
                process.stdout.close()
                after = signature(staged)
                with self.condition:
                    if job['status'] == 'stopping' and stop_succeeded:
                        job.update(status='stopped', error=None)
                    elif process.returncode == 0 and after is not None and after[2] > 0:
                        os.replace(staged, target)
                        job.update(status='done', error=None)
                    else:
                        job.update(status='failed', error='Не удалось создать MP4. Проверьте журнал сервиса.')
                    self.save()
            except Exception as exc:
                print('Conversion failed:', exc, flush=True)
                with self.condition:
                    job.update(status='stopped' if job['status'] == 'stopping' else 'failed',
                               error='Не удалось запустить конвертацию. Проверьте журнал сервиса.')
                    self.save()
            finally:
                finished.set()
                try:
                    self.remove_workspace(job_id, run_id)
                except OSError as exc:
                    print('Workspace cleanup failed:', exc, flush=True)


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
        if self.headers.get_content_type() != 'application/json':
            self.send_error(415)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192:
                raise ValueError('Некорректный размер запроса.')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('Некорректный запрос.')
            if self.path == '/jobs':
                if not isinstance(payload.get('url'), str):
                    raise ValueError('Передайте URL записи.')
                job_id = self.server.jobs.submit(payload['url'])
            else:
                match = re.fullmatch(r'/jobs/([a-f0-9]{32})/(stop|delete|recreate)', self.path)
                if not match:
                    self.send_error(404)
                    return
                job_id, action = match.groups()
                self.server.jobs.action(job_id, action)
        except KeyError:
            self.respond_json(404, {'error': 'Задание не найдено.'})
            return
        except (ValueError, UnicodeDecodeError) as exc:
            self.respond_json(400, {'error': str(exc)})
            return
        except OSError as exc:
            print('Job action failed:', exc, flush=True)
            self.respond_json(500, {'error': 'Ошибка работы с файлами. Проверьте журнал сервиса.'})
            return
        self.respond_json(202, {'id': job_id})

    def send_file(self, path, content_type, filename=None):
        try:
            file = path.open('rb')
        except FileNotFoundError:
            self.send_error(404)
            return
        with file:
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            if filename:
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.send_header('Content-Length', str(os.fstat(file.fileno()).st_size))
            self.send_header('Cache-Control', 'private, max-age=60' if content_type == 'image/jpeg' else 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            try:
                shutil.copyfileobj(file, self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/':
            self.send_file(ROOT / 'web.html', 'text/html; charset=utf-8')
        elif path == '/jobs':
            self.respond_json(200, self.server.jobs.list())
        elif path.startswith('/jobs/') and JOB_RE.fullmatch(path[len('/jobs/'):]):
            job = self.server.jobs.get(path[len('/jobs/'):])
            self.respond_json(200, job) if job else self.respond_json(404, {'error': 'Задание не найдено.'})
        elif path.startswith('/download/') and FILE_RE.fullmatch(path[len('/download/'):]):
            name = path[len('/download/'):]
            self.send_file(self.server.jobs.output / name, 'video/mp4', name)
        elif re.fullmatch(r'/preview/[a-f0-9]{32}\.jpg', path):
            job_id = path[len('/preview/'):-4]
            job = self.server.jobs.get(job_id)
            if job and job['preview']:
                self.send_file(self.server.jobs.state_dir / (job_id + '.jpg'), 'image/jpeg')
            else:
                self.send_error(404)
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
