#!/usr/bin/env python3
"""Trusted-network interface around the original one-off converter."""
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import subprocess
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from operations import Operations

ROOT = Path(__file__).resolve().parent
URL_RE = re.compile(r'^https?://[^/?#\s]+/playback/presentation/2\.3/([a-z0-9]{40}-[0-9]{13})(?:[?#][^\s]*)?$')
FILE_RE = re.compile(r'^[a-z0-9]{40}-[0-9]{13}\.mp4$')
JOB_RE = re.compile(r'^[a-f0-9]{32}$')
TRUSTED_NETWORKS = tuple(ipaddress.ip_network(net) for net in
                         ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
ACTIVE = {'queued', 'running', 'stopping'}
RESOLUTIONS = {'1280x800', '1920x1080', '2560x1440', '2560x1080', '3440x1440'}
LEGACY_RESOLUTIONS = {'640x360', '854x480', '1280x720'}
FRAME_RATES = {15, 24, 25, 30, 50, 60}
PRESETS = {'ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow'}
DEFAULT_SETTINGS = {'resolution': '1280x800', 'video_bitrate': None, 'audio_bitrate': 128, 'preset': 'medium', 'codec': 'h264', 'fps': 30}
MAX_ATTEMPTS = 3
RETRY_DELAY = 5
LOW_SPACE_BYTES = 1024 ** 3
PREPARE_TIMEOUT = int(os.environ.get('BBB_MP4_PREPARE_TIMEOUT', '600'))
STALL_TIMEOUT = int(os.environ.get('BBB_MP4_STALL_TIMEOUT', '600'))
CAPTURE_TIMEOUT_FACTOR = float(os.environ.get('BBB_MP4_CAPTURE_TIMEOUT_FACTOR', '2'))
CAPTURE_TIMEOUT_GRACE = int(os.environ.get('BBB_MP4_CAPTURE_TIMEOUT_GRACE', '1800'))
MAX_RESTARTS = 2
LOG_LIMIT = 16000


def validate_settings(values, base=None, allow_legacy=False):
    if not isinstance(values, dict) or set(values) - set(DEFAULT_SETTINGS):
        raise ValueError('Неизвестные параметры конвертации.')
    result = dict(DEFAULT_SETTINGS if base is None else base)
    for key, value in values.items():
        if key == 'video_bitrate' and (value is None or value in ('auto', 'авто')):
            value = None
        elif key in ('video_bitrate', 'audio_bitrate'):
            if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isascii() or not str(value).isdigit():
                raise ValueError('Битрейт должен быть целым числом в кбит/с.')
            value = int(value)
            minimum, maximum = (250, 50000) if key == 'video_bitrate' else (32, 512)
            if not minimum <= value <= maximum:
                raise ValueError(f'Битрейт {key} должен быть от {minimum} до {maximum} кбит/с.')
        elif key == 'fps':
            if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isascii() or not str(value).isdigit():
                raise ValueError('Недопустимая частота кадров.')
            value = int(value)
            if value not in FRAME_RATES:
                raise ValueError('Недопустимая частота кадров.')
        elif key == 'resolution':
            if not isinstance(value, str) or value not in (RESOLUTIONS | LEGACY_RESOLUTIONS if allow_legacy else RESOLUTIONS):
                raise ValueError('Недопустимое разрешение.')
        elif not isinstance(value, str) or value not in ({'h264', 'h265'} if key == 'codec' else PRESETS):
            raise ValueError('Недопустимый кодек или пресет кодирования.')
        result[key] = value
    return result


def parse_duration(value):
    """Optional explicit recording length, in seconds or H:MM:SS."""
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise ValueError('Неверная длительность записи.')
    text = str(value).strip()
    if re.fullmatch(r'[0-9]{1,6}', text):
        seconds = int(text)
    elif re.fullmatch(r'[0-9]{1,3}:[0-5][0-9]:[0-5][0-9]', text):
        hours, minutes, seconds = map(int, text.split(':'))
        seconds += hours * 3600 + minutes * 60
    else:
        raise ValueError('Длительность указывается в секундах или формате Ч:ММ:СС.')
    if not 1 <= seconds <= 604800:
        raise ValueError('Длительность должна быть от 1 секунды до 7 суток.')
    return seconds


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # Persist the directory entry as well as the file contents on Linux.
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
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


def image_reference():
    path = ROOT / 'state' / 'image.ref'
    if not path.exists():
        return 'manishkatyan/bbb-mp4'
    image = path.read_text(encoding='ascii').strip()
    if not re.fullmatch(r'manishkatyan/bbb-mp4@sha256:[a-f0-9]{64}', image):
        raise ValueError('Некорректная закреплённая версия образа.')
    return image


def signature(path):
    try:
        stat = path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    except FileNotFoundError:
        return None


def mp4_duration(path):
    """Read movie duration from the bounded MP4 atom tree without decoding."""
    try:
        with path.open('rb') as f:
            size = os.fstat(f.fileno()).st_size
            def boxes(start, end):
                position = start
                for _ in range(100000):
                    if position + 8 > end:
                        return
                    f.seek(position)
                    header = f.read(8)
                    if len(header) != 8:
                        return
                    length, kind = struct.unpack('>I4s', header)
                    header_size = 8
                    if length == 1:
                        ext = f.read(8)
                        if len(ext) != 8:
                            return
                        length, header_size = struct.unpack('>Q', ext)[0], 16
                    elif length == 0:
                        length = end - position
                    if length < header_size or position + length > end:
                        return
                    yield kind, position + header_size, position + length
                    position += length
            for kind, start, end in boxes(0, size):
                if kind != b'moov':
                    continue
                for sub, mv_start, mv_end in boxes(start, end):
                    if sub != b'mvhd':
                        continue
                    f.seek(mv_start)
                    data = f.read(min(mv_end - mv_start, 32))
                    if len(data) >= 20 and data[0] == 0:
                        scale, duration = struct.unpack('>II', data[12:20])
                    elif len(data) >= 32 and data[0] == 1:
                        scale, duration = struct.unpack('>IQ', data[20:32])
                    else:
                        return None
                    if scale and duration not in (0, 0xffffffff, 0xffffffffffffffff):
                        return duration / scale
                    return None
    except (OSError, ValueError, struct.error):
        return None
    return None


class ConfirmationRequired(ValueError):
    pass


class ConversionError(RuntimeError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class OrphanContainerError(RuntimeError):
    """Do not start another job until container removal is confirmed."""


def remove_container(name):
    try:
        result = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OrphanContainerError('Не удалось подтвердить удаление контейнера ' + name) from exc
    if result.returncode and 'No such container' not in result.stderr:
        raise OrphanContainerError('Не удалось удалить контейнер ' + name + ': ' + result.stderr.strip())



class Jobs:
    def __init__(self, output, state_dir=None, screenshot_interval=60, retry_interval=5):
        self.output = Path(output)
        self.state_dir = Path(state_dir or ROOT / 'state')
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.state_file = self.state_dir / 'jobs.json'
        self.settings_file = self.state_dir / 'settings.json'
        self.settings = validate_settings(json.loads(self.settings_file.read_text(encoding='utf-8')), allow_legacy=True) if self.settings_file.exists() else dict(DEFAULT_SETTINGS)
        self.condition = threading.Condition()
        self.screenshot_interval = screenshot_interval
        self.retry_interval = retry_interval
        self.jobs = {}
        self.persistence_error = None
        self.worker_error = None
        self.maintenance = False
        self.file_metadata = {}
        if self.state_file.exists():
            # Fail closed on corrupt state, rather than lose jobs or overwrite files.
            data = json.loads(self.state_file.read_text(encoding='utf-8'))
            for job in data:
                if not JOB_RE.fullmatch(job['id']) or not FILE_RE.fullmatch(job['meeting_id'] + '.mp4'):
                    raise ValueError('Некорректный файл состояния заданий')
                for key in ('run_id', 'cleanup_run_id'):
                    if job.get(key) is not None and not JOB_RE.fullmatch(job[key]):
                        raise ValueError('Некорректный идентификатор запуска')
                # Older jobs retain the original image's encoding settings.
                if job.get('settings') is not None:
                    job['settings'] = validate_settings(job['settings'], allow_legacy=True)
                self.jobs[job['id']] = job
        # Finish a deletion that was durably requested before a crash.
        for job_id, job in list(self.jobs.items()):
            if job.get('delete_requested'):
                self.remove_result(job['meeting_id'])
                (self.state_dir / (job_id + '.jpg')).unlink(missing_ok=True)
                del self.jobs[job_id]
        self.recover()
        self.import_files()
        with self.condition:
            self.save()
        self.operations = Operations(self)
        threading.Thread(target=self.worker_guard, daemon=True, name='conversion-worker').start()

    def save(self):
        """Call with condition held; state includes URLs and is private to the service user."""
        write_json(self.state_file, list(self.jobs.values()))

    def save_worker(self):
        """Persist before proceeding; release lock while storage is unavailable."""
        while True:
            try:
                self.save()
                self.persistence_error = None
                return
            except OSError as exc:
                self.persistence_error = str(exc)
                print('Очередь ожидает восстановления записи состояния:', exc, flush=True)
                self.condition.wait(timeout=5)

    def worker_guard(self):
        while True:
            try:
                self.worker()
            except Exception as exc:
                self.worker_error = str(exc)
                print('Ошибка обработчика очереди; восстановление:', exc, flush=True)
                # Never run the next job while an orphaned conversion may exist.
                while True:
                    try:
                        with self.condition:
                            self.recover()
                            self.save_worker()
                        self.worker_error = None
                        break
                    except Exception as recovery_error:
                        self.worker_error = str(recovery_error)
                        print('Восстановление очереди не удалось:', recovery_error, flush=True)
                        time.sleep(5)

    def set_maintenance(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('Неверное значение режима обновления.')
        with self.condition:
            if enabled and self.maintenance:
                raise ValueError('Другой процесс уже обновляет сервис.')
            if enabled and any(j['status'] in ('running', 'stopping') for j in self.jobs.values()):
                raise ValueError('Идёт конвертация: обновление отложено до её завершения.')
            self.maintenance = enabled
            self.condition.notify_all()

    def get_settings(self):
        with self.condition:
            return dict(self.settings)

    def set_settings(self, values):
        with self.condition:
            # Older clients omit FPS; keep its current value, never reset it.
            if not isinstance(values, dict) or not (set(DEFAULT_SETTINGS) - {'fps'}) <= set(values) or set(values) - set(DEFAULT_SETTINGS):
                raise ValueError('Передайте все глобальные параметры.')
            # A previously saved low resolution remains usable but cannot be newly selected.
            allow_legacy = values.get('resolution') == self.settings['resolution']
            result = validate_settings(values, self.settings, allow_legacy=allow_legacy)
            write_json(self.settings_file, result)
            self.settings = result
            return dict(result)

    def disk_info(self):
        volumes = []
        paths = [('results', self.output), ('state', self.state_dir)]
        # Never infer Docker configuration. An operator can supply the actual
        # storage path; an unset path is explicitly reported as unknown.
        container_path = os.environ.get('BBB_MP4_CONTAINER_STORAGE')
        if not container_path and hasattr(self, 'operations'):
            try:
                container_path = self.operations.config().get('container_storage')
            except (OSError, ValueError):
                pass
        paths.append(('container', Path(container_path) if isinstance(container_path, str) and os.path.isabs(container_path) else None))
        for name, path in paths:
            if path is None:
                volumes.append({'name': name, 'free_bytes': None, 'low': False})
                continue
            try:
                free = shutil.disk_usage(path).free
                volumes.append({'name': name, 'free_bytes': free, 'low': free < LOW_SPACE_BYTES})
            except OSError:
                volumes.append({'name': name, 'free_bytes': None, 'low': False})
        return {'free_bytes': volumes[0]['free_bytes'], 'low': any(v['low'] for v in volumes),
                'threshold_bytes': LOW_SPACE_BYTES, 'unknown_required': any(v['free_bytes'] is None for v in volumes if v['name'] in ('results', 'state')), 'volumes': volumes}

    def log(self, job, text):
        job['log_tail'] = (job.get('log_tail', '') + text)[-LOG_LIMIT:]

    def get_log(self, job_id):
        with self.condition:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            return job.get('log_tail', '')

    def recover(self):
        # First stop orphans. A requested stop must never be turned into a restart.
        for job in self.jobs.values():
            if job['status'] in ('running', 'stopping'):
                for prefix in ('bbb-mp4-', 'bbb-probe-'):
                    remove_container(prefix + job['id'])
                target = self.output / (job['meeting_id'] + '.mp4')
                stage = self.workspace(job['id'], job['run_id']) / target.name if job.get('run_id') else None
                if job['status'] == 'stopping':
                    job.update(status='stopped', phase=None, error='Остановлено при перезапуске сервиса.')
                else:
                    # The file may already have been published before the state
                    # write. Validate it (or the staged file) before recapturing.
                    candidate = 'target' if job.get('phase') == 'verifying' and target.is_file() else 'staged' if stage and stage.is_file() else None
                    restarts = job.get('restart_count', 0) + 1
                    if restarts <= MAX_RESTARTS or candidate:
                        job.update(status='queued', queued_at=time.time_ns(), next_attempt=0,
                                   restart_count=restarts, recovery_source=candidate,
                                   error='Продолжение после перезапуска сервиса.', attempt=0)
                    else:
                        job.update(status='failed', phase=None, error='Превышено число повторов после перезапуска сервиса.')
                if not job.get('recovery_source') and job.get('run_id'):
                    self.remove_workspace(job['id'], job['run_id'])
            elif job.get('run_id') and not job.get('recovery_source'):
                self.remove_workspace(job['id'], job['run_id'])
            if job.get('cleanup_run_id'):
                self.remove_workspace(job['id'], job['cleanup_run_id'])
                job.pop('cleanup_run_id', None)

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
        item.pop('cleanup_run_id', None)
        item.pop('recovery_source', None)
        item.pop('log_tail', None)
        target = self.output / (job['meeting_id'] + '.mp4')
        item['has_file'] = target.is_file()
        if job.get('delete_requested'):
            item['error'] = 'Удаление не завершено. Повторите удаление задания.'
        item['file_size'] = item['file_duration'] = None
        if item['has_file']:
            current = signature(target)
            if current:
                cached = self.file_metadata.get(target.name)
                if not cached or cached[0] != current:
                    cached = (current, mp4_duration(target))
                    self.file_metadata[target.name] = cached
                item['file_size'] = current[2]
                item['file_duration'] = cached[1] or (job.get('verification') or {}).get('duration') or job.get('duration')
        item['download'] = '/download/' + target.name if item['has_file'] else None
        if job['status'] == 'done' and not item['has_file'] and not job.get('delete_requested'):
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

    def check_queue_capacity(self):
        if sum(j.get('status') == 'queued' for j in self.jobs.values()) >= 100:
            raise ValueError('Очередь заполнена. Попробуйте позже.')

    def remove_result(self, meeting_id):
        target = self.output / (meeting_id + '.mp4')
        target.unlink(missing_ok=True)
        self.file_metadata.pop(target.name, None)

    def enqueue(self, job, remove_existing=False):
        self.check_queue_capacity()
        previous = dict(job)
        job.update(status='queued', queued_at=time.time_ns(), run_id=secrets.token_hex(16), duration=None,
                   capture_started=None, screenshot_time=None, error=None, phase=None, log_tail='', space_warning=False,
                   verification=None, attempt=0, next_attempt=0, restart_count=0, recovery_source=None,
                   delete_before_run=remove_existing)
        try:
            # Persist the deletion intent before touching the previous file.
            self.save()
        except Exception:
            job.clear()
            job.update(previous)
            raise
        if remove_existing:
            try:
                self.remove_result(job['meeting_id'])
                job['delete_before_run'] = False
                self.save()
            except OSError as exc:
                # The durable intent ensures restart/worker will retry deletion
                # before starting. Never silently run over the previous result.
                job['delete_before_run'] = True
                self.log(job, 'Ожидание удаления прежнего MP4: ' + str(exc) + '\n')
        try:
            (self.state_dir / (job['id'] + '.jpg')).unlink(missing_ok=True)
        except OSError as exc:
            self.log(job, 'Не удалось удалить прежнее превью: ' + str(exc) + '\n')
        self.condition.notify()

    def submit(self, url, overrides=None, replace_confirmed=False, duration_override=None):
        match = URL_RE.fullmatch(url)
        if not match:
            raise ValueError('Неверный playback URL BigBlueButton 2.3.')
        meeting_id = match.group(1)
        duration_override = parse_duration(duration_override)
        with self.condition:
            if self.maintenance:
                raise ValueError('Сервис обновляется. Попробуйте позже.')
            settings = validate_settings(overrides if overrides is not None else {}, self.settings)
            existing = next((j for j in self.jobs.values() if j['meeting_id'] == meeting_id), None)
            if existing:
                if existing['url'] is None and existing['status'] not in ACTIVE:
                    self.check_queue_capacity()
                    if (self.output / (meeting_id + '.mp4')).exists():
                        if replace_confirmed is not True:
                            raise ConfirmationRequired('Предыдущий MP4 будет удалён сразу. Продолжить?')
                    previous = dict(existing)
                    existing['url'] = url
                    existing['settings'] = settings
                    existing['duration_override'] = duration_override
                    try:
                        self.enqueue(existing, remove_existing=True)
                    except Exception:
                        existing.clear()
                        existing.update(previous)
                        raise
                    return existing['id']
                raise ValueError('Эта запись уже есть в списке заданий.')
            self.check_queue_capacity()
            if (self.output / (meeting_id + '.mp4')).exists():
                if replace_confirmed is not True:
                    raise ConfirmationRequired('Предыдущий MP4 будет удалён сразу. Продолжить?')
            job_id = secrets.token_hex(16)
            job = {'id': job_id, 'meeting_id': meeting_id, 'url': url, 'created': time.time(), 'settings': settings, 'duration_override': duration_override}
            self.jobs[job_id] = job
            try:
                self.enqueue(job, remove_existing=True)
            except Exception:
                del self.jobs[job_id]
                raise
            return job_id

    def action(self, job_id, action, duration_override=...):
        with self.condition:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            if job.get('delete_requested') and action != 'delete':
                raise ValueError('Удаление не завершено. Повторите удаление задания.')
            if action == 'stop':
                if job['status'] == 'queued':
                    job.update(status='stopped', error=None)
                elif job['status'] == 'running':
                    job['status'] = 'stopping'
                else:
                    raise ValueError('Это задание сейчас нельзя остановить.')
                self.save()
            elif action == 'recreate':
                if self.maintenance:
                    raise ValueError('Сервис обновляется. Попробуйте позже.')
                if job['status'] in ACTIVE:
                    raise ValueError('Дождитесь завершения или остановите задание.')
                if not job.get('url'):
                    raise ValueError('Для старого файла неизвестен URL. Вставьте его в форму выше.')
                self.check_queue_capacity()
                previous_duration = job.get('duration_override')
                if duration_override is not ...:
                    job['duration_override'] = parse_duration(duration_override)
                try:
                    self.enqueue(job, remove_existing=True)
                except Exception:
                    job['duration_override'] = previous_duration
                    raise
            elif action == 'delete':
                if job['status'] in ACTIVE:
                    raise ValueError('Сначала остановите задание.')
                # Persist the explicit deletion before modifying either file.
                job['delete_requested'] = True
                try:
                    self.save()
                except Exception:
                    job.pop('delete_requested', None)
                    raise
                self.remove_result(job['meeting_id'])
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

    def inspect_container(self, job_id, staged, program, arguments, timeout, return_stderr=False):
        """Run a cancellable, named inspection container with read-only output."""
        name = 'bbb-probe-' + job_id
        command = ['docker', 'run', '--rm', '--name', name, '--entrypoint', program,
                   '-v', str(staged.parent) + ':/probe:ro', image_reference()] + arguments
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace')
        deadline = time.monotonic() + timeout
        stop_requested_at = None
        while True:
            try:
                stdout, stderr = process.communicate(timeout=.5)
                break
            except subprocess.TimeoutExpired:
                with self.condition:
                    stopping = self.jobs[job_id]['status'] == 'stopping'
                if stopping:
                    if stop_requested_at is None:
                        stop_requested_at = time.monotonic()
                    if time.monotonic() - stop_requested_at > 30:
                        process.kill()
                        process.communicate()
                        remove_container(name)
                        return None
                    try:
                        subprocess.run(['docker', 'stop', '--time', '2', name], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=10)
                    except (OSError, subprocess.SubprocessError):
                        pass
                if time.monotonic() >= deadline:
                    process.kill()
                    process.communicate()
                    remove_container(name)
                    raise ConversionError('Превышено время проверки MP4.', retryable=True)
        with self.condition:
            stopping = self.jobs[job_id]['status'] == 'stopping'
        if stopping:
            remove_container(name)
            return None
        if process.returncode:
            raise ConversionError(program + ' не смог проверить MP4: ' + (stderr.strip()[-500:] or f'код {process.returncode}'), process.returncode not in (125, 126, 127))
        return stderr if return_stderr else stdout

    def probe(self, job_id, staged, settings):
        """Check the container/streams and decode the entire new MP4."""
        file = '/probe/' + staged.name
        stdout = self.inspect_container(job_id, staged, 'ffprobe', [
            '-v', 'error', '-show_entries', 'format=format_name,duration:stream=codec_type,codec_name,width,height',
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
                if video.get('codec_name') != ('hevc' if settings['codec'] == 'h265' else 'h264'):
                    raise ValueError('кодек MP4 не соответствует настройкам задания')
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ConversionError('MP4 не прошёл проверку: ' + str(exc), not any(key in str(exc) for key in ('разрешение', 'кодек'))) from exc
        with self.condition:
            self.log(self.jobs[job_id], 'Проверка декодирования видео и аудио…\n')
        threshold = max(20, duration * .8)
        decoded = self.inspect_container(job_id, staged, 'ffmpeg', [
            '-nostats', '-v', 'info', '-xerror', '-i', file, '-map', '0:v:0', '-map', '0:a:0',
            '-vf', f'blackdetect=d={threshold:.3f}:pix_th=0.10',
            '-af', f'silencedetect=noise=-50dB:d={threshold:.3f}', '-f', 'null', '-'],
            max(300, duration * 4 + 60), return_stderr=True)
        if decoded is None:
            return None
        warnings = []
        if re.search(r'black_duration:\s*([0-9.]+)', decoded):
            warnings.append('Большую часть записи изображение может быть чёрным.')
        if re.search(r'silence_duration:\s*([0-9.]+)', decoded):
            warnings.append('Большую часть записи может отсутствовать звук.')
        return {'duration': duration, 'warnings': warnings}

    def worker(self):
        while True:
            with self.condition:
                if self.maintenance:
                    self.condition.wait()
                    continue
                queued = [j for j in self.jobs.values() if j['status'] == 'queued']
                now = time.time()
                ready = [j for j in queued if j.get('next_attempt', 0) <= now]
                if not ready:
                    delay = max(0.01, min(j.get('next_attempt', 0) for j in queued) - now) if queued else None
                    self.condition.wait(timeout=delay)
                    continue
                job = min(ready, key=lambda j: j.get('queued_at', 0))
                if job.get('delete_before_run'):
                    try:
                        self.remove_result(job['meeting_id'])
                        job['delete_before_run'] = False
                    except OSError as exc:
                        job.update(status='failed', error='Не удалось удалить прежний MP4: ' + str(exc)[:500])
                        self.save_worker()
                        continue
                job['status'] = 'running'
                job['attempt'] = job.get('attempt', 0) + 1
                job['error'] = None
                job['phase'] = 'preparing'
                self.log(job, f"\nПопытка {job['attempt']} из {MAX_ATTEMPTS}.\n")
                try:
                    disk = self.disk_info()
                    job['space_warning'] = disk['low'] or disk['unknown_required']
                except OSError as exc:
                    job['space_warning'] = True
                    self.log(job, 'Не удалось проверить свободное место: ' + str(exc) + '\n')
                if job['space_warning']:
                    self.log(job, 'Внимание: свободного места меньше 1 ГиБ или его не удалось проверить.\n')
                self.save_worker()
                job_id, run_id, url = job['id'], job['run_id'], job['url']
                target = self.output / (job['meeting_id'] + '.mp4')
            finished = threading.Event()
            workspace = self.workspace(job_id, run_id)
            orphaned = False
            try:
                recovery_source = job.pop('recovery_source', None)
                if recovery_source:
                    candidate = target if recovery_source == 'target' else workspace / target.name
                    with self.condition:
                        job['phase'] = 'verifying'
                        self.log(job, 'Проверка файла после перезапуска сервиса…\n')
                        self.save_worker()
                    verification = self.probe(job_id, candidate, job.get('settings'))
                    with self.condition:
                        if job['status'] == 'stopping' or verification is None:
                            job.update(status='stopped', phase=None, error=None)
                        else:
                            expected = job.get('duration') or job.get('duration_override')
                            if not expected:
                                raise ConversionError('Нельзя подтвердить полноту восстановленного MP4: длительность записи неизвестна.', retryable=True)
                            if verification['duration'] + max(2, expected * .01) < expected:
                                raise ConversionError('Восстановленный MP4 короче записи.', retryable=True)
                            if candidate != target:
                                os.replace(candidate, target)
                            job.update(status='done', phase=None, verification=verification, error=None)
                            self.log(job, 'Файл после перезапуска проверен и восстановлен.\n')
                        self.save_worker()
                    continue
                self.remove_workspace(job_id, run_id)
                workspace.mkdir(mode=0o700)
                staged = workspace / target.name
                env = os.environ.copy()
                env['BBB_MP4_OUTPUT_LOCATION'] = str(workspace)
                env['BBB_MP4_CONTAINER_NAME'] = 'bbb-mp4-' + job_id
                if job.get('duration_override'):
                    env['BBB_MP4_DURATION'] = str(job['duration_override'])
                else:
                    env.pop('BBB_MP4_DURATION', None)
                settings = job.get('settings')
                if settings is not None:
                    env.update(BBB_MP4_SIZE=settings['resolution'],
                               BBB_MP4_VIDEO_BITRATE='auto' if settings['video_bitrate'] is None else str(settings['video_bitrate']),
                               BBB_MP4_CODEC=settings['codec'],
                               BBB_MP4_AUDIO_BITRATE=str(settings['audio_bitrate']),
                               BBB_MP4_PRESET=settings['preset'], BBB_MP4_FPS=str(settings['fps']))
                else:
                    for key in ('BBB_MP4_SIZE', 'BBB_MP4_VIDEO_BITRATE', 'BBB_MP4_AUDIO_BITRATE', 'BBB_MP4_PRESET', 'BBB_MP4_CODEC', 'BBB_MP4_FPS'):
                        env.pop(key, None)
                process = subprocess.Popen([str(ROOT / 'convert.sh'), url], cwd=ROOT, env=env,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, errors='replace', bufsize=1, start_new_session=True)
                reader = threading.Thread(target=self.log_reader, args=(process, job_id, run_id), daemon=True)
                reader.start()
                threading.Thread(target=self.screenshots, args=(job_id, run_id, finished), daemon=True).start()
                stop_succeeded = False
                started_at = time.time()
                timed_out = False
                last_progress = None
                last_activity = time.monotonic()
                capture_seen = False
                timeout_reason = 'Превышено допустимое время конвертации'
                stop_requested_at = None
                while process.poll() is None:
                    with self.condition:
                        stopping = self.jobs[job_id]['status'] == 'stopping'
                        duration = job.get('duration')
                        capture_started = job.get('capture_started')
                        phase = job.get('phase')
                    if capture_started and not capture_seen:
                        last_activity = time.monotonic()
                        capture_seen = True
                    progress_signature = signature(workspace / '.ffmpeg-progress') if settings is not None else None
                    if progress_signature != last_progress:
                        last_progress = progress_signature
                        last_activity = time.monotonic()
                    stalled = bool(settings is not None and capture_started and phase == 'capturing'
                                   and time.monotonic() - last_activity > STALL_TIMEOUT)
                    elapsed = time.time() - (capture_started or started_at)
                    limit = max(duration * CAPTURE_TIMEOUT_FACTOR + CAPTURE_TIMEOUT_GRACE, duration + 600) if duration and capture_started else PREPARE_TIMEOUT
                    timed_out = not stopping and (elapsed > limit or stalled)
                    if stalled:
                        timeout_reason = 'Нет прогресса FFmpeg в течение допустимого времени'
                    if stopping and stop_requested_at is None:
                        stop_requested_at = time.monotonic()
                    force_stop = stopping and time.monotonic() - stop_requested_at > 30
                    if timed_out or force_stop:
                        # Kill the entire client process group before removing
                        # the named container, preventing a late launch race.
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                            process.wait(timeout=10)
                        except (ProcessLookupError, subprocess.TimeoutExpired):
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            process.wait()
                        remove_container('bbb-mp4-' + job_id)
                        if force_stop:
                            stop_succeeded = True
                        break
                    if stopping:
                        # Retry if requested before docker has created the named container.
                        try:
                            stop = subprocess.run(['docker', 'stop', '--time', '5', 'bbb-mp4-' + job_id],
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                            stop_succeeded = stop_succeeded or stop.returncode == 0
                        except (OSError, subprocess.SubprocessError) as exc:
                            print('Container stop failed:', exc, flush=True)
                    time.sleep(.5)
                reader.join(timeout=15)
                process.stdout.close()
                if timed_out:
                    raise ConversionError(timeout_reason + '; контейнер остановлен.', retryable=True)
                finished.set()
                after = signature(staged)
                with self.condition:
                    stopping = job['status'] == 'stopping'
                    if stopping:
                        if not stop_succeeded and process.returncode != 0:
                            remove_container('bbb-mp4-' + job_id)
                        job.update(status='stopped', error=None)
                        self.save_worker()
                        continue
                    permanent = any(text in job.get('log_tail', '') for text in ('Recording not found!', 'Invalid recording URL'))
                    if process.returncode != 0 and 'Не удалось получить длительность записи' in job.get('log_tail', ''):
                        raise ConversionError('Не удалось получить длительность записи. Укажите её вручную при пересоздании.', retryable=False)
                    if process.returncode != 0:
                        raise ConversionError(f'Контейнер конвертации завершился с кодом {process.returncode}.', not permanent and process.returncode not in (125, 126, 127))
                    if after is None or after[2] == 0:
                        raise ConversionError('Конвертер не создал непустой MP4. Проверьте сообщения FFmpeg в журнале.', not permanent)
                    job['phase'] = 'verifying'
                    self.log(job, '\nПроверка MP4: метаданные и декодирование…\n')
                    self.save_worker()
                verification = self.probe(job_id, staged, settings)
                with self.condition:
                    if job['status'] == 'stopping' or verification is None:
                        job.update(status='stopped', error=None)
                    else:
                        expected = job.get('duration')
                        if expected and verification['duration'] + max(2, expected * .01) < expected:
                            raise ConversionError('MP4 короче записи: конвертация могла прерваться.', retryable=True)
                        os.replace(staged, target)
                        job.update(status='done', phase=None, verification=verification, error=None)
                        self.log(job, 'MP4: контейнер, длительность и декодирование видео- и аудиодорожек проверены.\n')
                        for warning in verification.get('warnings', []):
                            self.log(job, 'Внимание: ' + warning + ' Проверьте запись вручную.\n')
                    self.save_worker()
            except OrphanContainerError:
                orphaned = True
                raise
            except Exception as exc:
                print('Conversion failed:', exc, flush=True)
                with self.condition:
                    self.log(job, '\nОшибка: ' + str(exc) + '\n')
                    retry = (job['status'] != 'stopping' and isinstance(exc, ConversionError)
                             and exc.retryable and job.get('attempt', 0) < MAX_ATTEMPTS)
                    if retry:
                        delay = RETRY_DELAY * (2 ** (job['attempt'] - 1))
                        job.update(status='queued', phase=None, error='Повтор после ошибки: ' + str(exc)[:600],
                                   next_attempt=time.time() + delay, queued_at=time.time_ns(),
                                   run_id=secrets.token_hex(16), cleanup_run_id=run_id, duration=None, capture_started=None,
                                   screenshot_time=None, verification=None)
                        (self.state_dir / (job_id + '.jpg')).unlink(missing_ok=True)
                        self.log(job, f'Повтор через {delay:g} с.\n')
                    else:
                        job.update(status='failed', phase=None, error=str(exc)[:700])
                    self.save_worker()
            finally:
                finished.set()
                try:
                    if not orphaned:
                        self.remove_workspace(job_id, run_id)
                    with self.condition:
                        current = self.jobs.get(job_id)
                        if current and current.get('cleanup_run_id') == run_id:
                            current.pop('cleanup_run_id', None)
                            self.save_worker()
                except OSError as exc:
                    print('Workspace cleanup failed:', exc, flush=True)
                with self.condition:
                    current = self.jobs.get(job_id)
                    event = dict(current) if current and current.get('run_id') == run_id and current['status'] in ('done', 'failed') else None
                if event:
                    self.operations.completed(event)


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
                job_id = self.server.jobs.submit(payload['url'], payload.get('settings', {}), payload.get('replace_confirmed', False), payload.get('duration_override'))
            elif self.path == '/settings':
                self.respond_json(200, self.server.jobs.set_settings(payload))
                return
            elif self.path == '/maintenance':
                self.server.jobs.set_maintenance(payload.get('enabled'))
                self.respond_json(200, {'maintenance': payload['enabled']})
                return
            elif self.path == '/backup':
                self.server.jobs.operations.schedule_backup()
                self.respond_json(202, {'message': 'Резервное копирование запланировано.'})
                return
            else:
                match = re.fullmatch(r'/jobs/([a-f0-9]{32})/(stop|delete|recreate)', self.path)
                if not match:
                    self.send_error(404)
                    return
                job_id, action = match.groups()
                self.server.jobs.action(job_id, action, payload.get('duration_override', ...))
        except ConfirmationRequired as exc:
            self.respond_json(409, {'error': str(exc), 'requires_confirmation': True})
            return
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
        elif path == '/health':
            jobs = self.server.jobs
            self.respond_json(200, {'persistence_error': jobs.persistence_error, 'worker_error': jobs.worker_error, 'maintenance': jobs.maintenance, 'operations': jobs.operations.health()})
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
