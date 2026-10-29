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
RESOLUTIONS = {'640x360', '854x480', '1280x720', '1280x800', '1920x1080'}
PRESETS = {'ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow'}
DEFAULT_SETTINGS = {'resolution': '1280x800', 'video_bitrate': 2500, 'audio_bitrate': 128, 'preset': 'fast'}
LOW_SPACE_BYTES = 1024 ** 3
LOG_LIMIT = 16000


def validate_settings(values, base=None):
    if not isinstance(values, dict) or set(values) - set(DEFAULT_SETTINGS):
        raise ValueError('Неизвестные параметры конвертации.')
    result = dict(DEFAULT_SETTINGS if base is None else base)
    for key, value in values.items():
        if key in ('video_bitrate', 'audio_bitrate'):
            if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isascii() or not str(value).isdigit():
                raise ValueError('Битрейт должен быть целым числом в кбит/с.')
            value = int(value)
            minimum, maximum = (250, 50000) if key == 'video_bitrate' else (32, 512)
            if not minimum <= value <= maximum:
                raise ValueError(f'Битрейт {key} должен быть от {minimum} до {maximum} кбит/с.')
        elif not isinstance(value, str) or value not in (RESOLUTIONS if key == 'resolution' else PRESETS):
            raise ValueError('Недопустимое разрешение или пресет кодирования.')
        result[key] = value
    return result


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
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
        self.settings_file = self.state_dir / 'settings.json'
        self.settings = validate_settings(json.loads(self.settings_file.read_text(encoding='utf-8'))) if self.settings_file.exists() else dict(DEFAULT_SETTINGS)
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
                # Older jobs retain the original image's encoding settings.
                if job.get('settings') is not None:
                    job['settings'] = validate_settings(job['settings'])
                self.jobs[job['id']] = job
        self.recover()
        self.import_files()
        with self.condition:
            self.save()
        threading.Thread(target=self.worker, daemon=True).start()

    def save(self):
        """Call with condition held; state includes URLs and is private to the service user."""
        write_json(self.state_file, list(self.jobs.values()))

    def get_settings(self):
        with self.condition:
            return dict(self.settings)

    def set_settings(self, values):
        with self.condition:
            if not isinstance(values, dict) or set(values) != set(DEFAULT_SETTINGS):
                raise ValueError('Передайте все глобальные параметры.')
            result = validate_settings(values)
            write_json(self.settings_file, result)
            self.settings = result
            return dict(result)

    def disk_info(self):
        free = shutil.disk_usage(self.output).free
        return {'free_bytes': free, 'low': free < LOW_SPACE_BYTES, 'threshold_bytes': LOW_SPACE_BYTES}

    def log(self, job, text):
        job['log_tail'] = (job.get('log_tail', '') + text)[-LOG_LIMIT:]

    def get_log(self, job_id):
        with self.condition:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            return job.get('log_tail', '')

    def recover(self):
        # A restarted docker client may have left a container running. Do not
        # start another job until it is removed, or claim it has stopped.
        for job in self.jobs.values():
            if job['status'] in ('running', 'stopping'):
                for prefix in ('bbb-mp4-', 'bbb-probe-'):
                    result = subprocess.run(['docker', 'rm', '-f', prefix + job['id']], capture_output=True, text=True, timeout=30)
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
        item.pop('log_tail', None)
        target = self.output / (job['meeting_id'] + '.mp4')
        item['has_file'] = target.is_file()
        item['download'] = '/download/' + target.name if item['has_file'] else None
        if job['status'] == 'done' and not item['has_file']:
            item['error'] = 'MP4-файл не найден на диске.'
        item['preview'] = '/preview/' + job['id'] + '.jpg' if job.get('screenshot_time') else None
        item['progress'] = item['remaining'] = None
        if job['status'] == 'done':
            item['progress'], item['remaining'] = 100, 0
        elif job['status'] in ('running', 'stopping') and job.get('phase') in ('finishing', 'verifying'):
            item['progress'] = 99
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
                   capture_started=None, screenshot_time=None, error=None, phase=None, log_tail='', space_warning=False, verification=None)
        (self.state_dir / (job['id'] + '.jpg')).unlink(missing_ok=True)
        self.save()
        self.condition.notify()

    def submit(self, url, overrides=None):
        match = URL_RE.fullmatch(url)
        if not match:
            raise ValueError('Неверный playback URL BigBlueButton 2.3.')
        meeting_id = match.group(1)
        with self.condition:
            settings = validate_settings(overrides if overrides is not None else {}, self.settings)
            existing = next((j for j in self.jobs.values() if j['meeting_id'] == meeting_id), None)
            if existing:
                if existing['url'] is None and existing['status'] not in ACTIVE:
                    existing['url'] = url
                    existing['settings'] = settings
                    self.enqueue(existing)
                    return existing['id']
                raise ValueError('Эта запись уже есть в списке заданий.')
            job_id = secrets.token_hex(16)
            job = {'id': job_id, 'meeting_id': meeting_id, 'url': url, 'created': time.time(), 'settings': settings}
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
                self.log(job, line)
                changed = False
                if re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value):
                    duration = float(value)
                    if math.isfinite(duration) and duration > 0 and job.get('duration') is None:
                        job['duration'] = duration
                        changed = True
                if 'Output #0, mp4, to ' in line and job.get('capture_started') is None:
                    job['capture_started'] = time.time()
                    job['phase'] = 'capturing'
                    changed = True
                if ('Starting second pass:' in line or 'Moving video to processed folder' in line) and job.get('phase') != 'finishing':
                    job['phase'] = 'finishing'
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

    def inspect_container(self, job_id, staged, program, arguments, timeout):
        """Run a cancellable, named inspection container with read-only output."""
        name = 'bbb-probe-' + job_id
        command = ['docker', 'run', '--rm', '--name', name, '--entrypoint', program,
                   '-v', str(staged.parent) + ':/probe:ro', 'manishkatyan/bbb-mp4'] + arguments
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace')
        deadline = time.monotonic() + timeout
        while True:
            try:
                stdout, stderr = process.communicate(timeout=.5)
                break
            except subprocess.TimeoutExpired:
                with self.condition:
                    stopping = self.jobs[job_id]['status'] == 'stopping'
                if stopping:
                    try:
                        subprocess.run(['docker', 'stop', '--time', '2', name], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=10)
                    except (OSError, subprocess.SubprocessError):
                        pass
                if time.monotonic() >= deadline:
                    process.kill()
                    process.communicate()
                    cleanup = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, text=True, timeout=30)
                    if cleanup.returncode and 'No such container' not in cleanup.stderr:
                        raise RuntimeError('Не удалось остановить контейнер проверки: ' + cleanup.stderr.strip())
                    raise RuntimeError('Превышено время проверки MP4.')
        with self.condition:
            stopping = self.jobs[job_id]['status'] == 'stopping'
        if stopping:
            cleanup = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, text=True, timeout=30)
            if cleanup.returncode and 'No such container' not in cleanup.stderr:
                raise RuntimeError('Не удалось подтвердить остановку контейнера проверки: ' + cleanup.stderr.strip())
            return None
        if process.returncode:
            raise RuntimeError(program + ' не смог проверить MP4: ' + (stderr.strip()[-500:] or f'код {process.returncode}'))
        return stdout

    def probe(self, job_id, staged, settings):
        """Check the container/streams and decode the entire new MP4."""
        file = '/probe/' + staged.name
        stdout = self.inspect_container(job_id, staged, 'ffprobe', [
            '-v', 'error', '-show_entries', 'format=format_name,duration:stream=codec_type,width,height',
            '-of', 'json', file], 60)
        if stdout is None:
            return None
        try:
            metadata = json.loads(stdout)
            fmt = metadata['format']
            duration = float(fmt['duration'])
            types = {stream.get('codec_type') for stream in metadata['streams']}
            if 'mp4' not in fmt['format_name'].split(',') or not math.isfinite(duration) or duration <= 0:
                raise ValueError('неверный формат или длительность')
            if not {'video', 'audio'} <= types:
                raise ValueError('отсутствует видео- или аудиодорожка')
            if settings is not None:
                video = next(stream for stream in metadata['streams'] if stream.get('codec_type') == 'video')
                expected = tuple(int(v) for v in settings['resolution'].split('x'))
                if (video.get('width'), video.get('height')) != expected:
                    raise ValueError('разрешение MP4 не соответствует настройкам задания')
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError('MP4 не прошёл проверку: ' + str(exc)) from exc
        with self.condition:
            self.log(self.jobs[job_id], 'Проверка декодирования видео и аудио…\n')
        decoded = self.inspect_container(job_id, staged, 'ffmpeg', [
            '-v', 'error', '-xerror', '-i', file, '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'],
            max(300, duration * 4 + 60))
        return None if decoded is None else {'duration': duration}

    def worker(self):
        while True:
            with self.condition:
                job = min((j for j in self.jobs.values() if j['status'] == 'queued'), key=lambda j: j.get('queued_at', 0), default=None)
                if not job:
                    self.condition.wait()
                    continue
                job['status'] = 'running'
                job['phase'] = 'preparing'
                try:
                    job['space_warning'] = self.disk_info()['low']
                except OSError as exc:
                    job['space_warning'] = True
                    self.log(job, 'Не удалось проверить свободное место: ' + str(exc) + '\n')
                if job['space_warning']:
                    self.log(job, 'Внимание: свободного места меньше 1 ГиБ или его не удалось проверить.\n')
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
                settings = job.get('settings')
                if settings is not None:
                    env.update(BBB_MP4_SIZE=settings['resolution'],
                               BBB_MP4_VIDEO_BITRATE=str(settings['video_bitrate']),
                               BBB_MP4_AUDIO_BITRATE=str(settings['audio_bitrate']),
                               BBB_MP4_PRESET=settings['preset'])
                else:
                    for key in ('BBB_MP4_SIZE', 'BBB_MP4_VIDEO_BITRATE', 'BBB_MP4_AUDIO_BITRATE', 'BBB_MP4_PRESET'):
                        env.pop(key, None)
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
                finished.set()
                after = signature(staged)
                with self.condition:
                    stopping = job['status'] == 'stopping'
                    if stopping and (stop_succeeded or process.returncode == 0):
                        job.update(status='stopped', error=None)
                        self.save()
                        continue
                    if process.returncode != 0:
                        raise RuntimeError(f'Контейнер конвертации завершился с кодом {process.returncode}.')
                    if after is None or after[2] == 0:
                        raise RuntimeError('Конвертер не создал непустой MP4. Проверьте сообщения FFmpeg в журнале.')
                    job['phase'] = 'verifying'
                    self.log(job, '\nПроверка MP4: метаданные и декодирование…\n')
                    self.save()
                verification = self.probe(job_id, staged, settings)
                with self.condition:
                    if job['status'] == 'stopping' or verification is None:
                        job.update(status='stopped', error=None)
                    else:
                        os.replace(staged, target)
                        job.update(status='done', phase=None, verification=verification, error=None)
                        self.log(job, 'MP4: контейнер, длительность и декодирование видео- и аудиодорожек проверены.\n')
                    self.save()
            except Exception as exc:
                print('Conversion failed:', exc, flush=True)
                with self.condition:
                    job.update(status='failed', phase=None, error=str(exc)[:700])
                    self.log(job, '\nОшибка: ' + str(exc) + '\n')
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
                job_id = self.server.jobs.submit(payload['url'], payload.get('settings', {}))
            elif self.path == '/settings':
                self.respond_json(200, self.server.jobs.set_settings(payload))
                return
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
        elif path == '/settings':
            self.respond_json(200, self.server.jobs.get_settings())
        elif path == '/disk':
            try:
                self.respond_json(200, self.server.jobs.disk_info())
            except OSError:
                self.respond_json(500, {'error': 'Не удалось проверить свободное место.'})
        elif re.fullmatch(r'/jobs/[a-f0-9]{32}/log', path):
            try:
                self.respond_json(200, {'log': self.server.jobs.get_log(path.split('/')[2])})
            except KeyError:
                self.respond_json(404, {'error': 'Задание не найдено.'})
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
