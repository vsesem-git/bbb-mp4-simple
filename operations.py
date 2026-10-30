"""Optional off-host backups and SMTP notifications; no extra daemon required."""
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage


class Operations:
    def __init__(self, jobs):
        self.jobs = jobs
        self.config_file = jobs.state_dir / 'operations.json'
        self.backup_status = 'Не настроено'
        self.notification_status = 'Не настроено'
        self._queue = queue.Queue()
        self._mail_queue = queue.Queue()
        self._pending = False
        self._dirty = False
        self._pending_lock = threading.Lock()
        threading.Thread(target=self._run, args=(self._queue,), daemon=True, name='backup-worker').start()
        threading.Thread(target=self._run, args=(self._mail_queue,), daemon=True, name='notification-worker').start()
        try:
            if self.config().get('backup_dir'):
                self.schedule_backup()
        except (OSError, ValueError) as exc:
            self.backup_status = 'Ошибка конфигурации: ' + str(exc)

    def config(self):
        if not self.config_file.exists():
            return {}
        if self.config_file.stat().st_mode & 0o077:
            raise ValueError('Права operations.json должны быть 600.')
        with self.config_file.open(encoding='utf-8') as f:
            config = json.load(f)
        if not isinstance(config, dict):
            raise ValueError('Некорректный operations.json.')
        return config

    def health(self):
        try:
            config = self.config()
            return {'backup_configured': bool(config.get('backup_dir')),
                    'notification_configured': bool(config.get('smtp')),
                    'backup_status': ('Ожидает выполнения' if config.get('backup_dir') and self.backup_status == 'Не настроено' else self.backup_status),
                    'notification_status': ('Ожидает события' if config.get('smtp') and self.notification_status == 'Не настроено' else self.notification_status)}
        except (OSError, ValueError) as exc:
            return {'backup_configured': False, 'notification_configured': False,
                    'backup_status': 'Ошибка конфигурации: ' + str(exc),
                    'notification_status': 'Ошибка конфигурации'}

    def schedule_backup(self):
        if not self.config().get('backup_dir'):
            raise ValueError('Резервное копирование не настроено в state/operations.json.')
        with self._pending_lock:
            if self._pending:
                self._dirty = True
                return
            self._pending = True
            self.backup_status = 'Ожидает выполнения'
            self._queue.put(('backup', None))

    def completed(self, job):
        try:
            config = self.config()
            if job['status'] == 'done' and config.get('backup_dir'):
                self.schedule_backup()
            if config.get('smtp'):
                # Do not send playback URLs or logs, which may contain tokens.
                self._mail_queue.put(('mail', {'meeting_id': job['meeting_id'], 'status': job['status']}))
        except (OSError, ValueError) as exc:
            self.backup_status = self.notification_status = 'Ошибка конфигурации: ' + str(exc)
            print(self.backup_status, flush=True)

    def _run(self, tasks):
        while True:
            kind, event = tasks.get()
            try:
                if kind == 'backup':
                    self.backup_status = 'Выполняется'
                    self.backup()
                    self.backup_status = 'Готово: ' + time.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    self.notify(event)
                    self.notification_status = 'Последнее уведомление отправлено'
            except Exception as exc:
                # Do not print SMTP errors: they can contain server responses or
                # credentials. Surface only the exception class to operators.
                message = 'Ошибка: ' + type(exc).__name__
                if kind == 'backup':
                    self.backup_status = message + ' — ' + str(exc)[:300]
                    print('Резервное копирование:', self.backup_status, flush=True)
                else:
                    self.notification_status = message
                    print('Уведомление:', message, flush=True)
            finally:
                if kind == 'backup':
                    with self._pending_lock:
                        if self._dirty:
                            self._dirty = False
                            self._queue.put(('backup', None))
                        else:
                            self._pending = False
                tasks.task_done()

    @staticmethod
    def _copy_stream(source, target):
        tmp = target.with_name('.' + target.name + '.' + secrets.token_hex(8) + '.tmp')
        try:
            with tmp.open('xb') as output:
                os.chmod(tmp, 0o600)
                shutil.copyfileobj(source, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)

    def backup(self):
        location = self.config().get('backup_dir')
        if not isinstance(location, str) or not os.path.isabs(location):
            raise ValueError('backup_dir должен быть абсолютным путём к точке монтирования.')
        root = Path(location)
        if not root.is_dir() or not os.path.ismount(root):
            raise ValueError('Внешнее хранилище не примонтировано по backup_dir.')
        device = root.stat().st_dev
        if device in (self.jobs.output.stat().st_dev, self.jobs.state_dir.stat().st_dev):
            raise ValueError('Резервная копия должна находиться на другой файловой системе.')
        dest = root / 'bbb-mp4-simple'
        recordings = dest / 'recordings'
        state = dest / 'state'
        for directory in (dest, recordings, state):
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        # Open completed files while locked. Recreate/delete may unlink them,
        # but on Unix an already-open descriptor keeps the verified bytes alive.
        sources = []
        try:
            with self.jobs.condition:
                snapshot = json.dumps(list(self.jobs.jobs.values()), ensure_ascii=False).encode('utf-8')
                settings = json.dumps(self.jobs.settings, ensure_ascii=False).encode('utf-8')
                for job in self.jobs.jobs.values():
                    if job['status'] == 'done':
                        path = self.jobs.output / (job['meeting_id'] + '.mp4')
                        try:
                            source = path.open('rb')
                            sources.append((source, recordings / path.name, os.fstat(source.fileno())))
                        except FileNotFoundError:
                            continue
                extras = []
                for path in self.jobs.state_dir.iterdir():
                    if path.name == 'image.ref' or (path.suffix == '.jpg' and len(path.stem) == 32):
                        try:
                            extras.append((path.name, path.read_bytes()))
                        except FileNotFoundError:
                            pass
            for source, target, stat in sources:
                existing = target.stat() if target.exists() else None
                if existing and existing.st_size == stat.st_size and existing.st_mtime_ns == stat.st_mtime_ns:
                    continue
                self._copy_stream(source, target)
                os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            import io
            for name, content in [('jobs.json', snapshot), ('settings.json', settings), *extras]:
                self._copy_stream(io.BytesIO(content), state / name)
        finally:
            for source, _, _ in sources:
                source.close()

    def notify(self, event):
        config = self.config().get('smtp')
        if not isinstance(config, dict):
            raise ValueError('Некорректные SMTP-настройки.')
        host, sender, receiver = config['host'], config['from'], config['to']
        port = int(config.get('port', 465))
        mode = config.get('mode', 'ssl')
        if mode not in ('ssl', 'starttls') or not 1 <= port <= 65535:
            raise ValueError('Допускаются только SMTP SSL или STARTTLS.')
        msg = EmailMessage()
        msg['From'], msg['To'] = sender, receiver
        msg['Subject'] = 'BBB MP4: ' + ('готово' if event['status'] == 'done' else 'ошибка')
        msg.set_content('Запись: ' + event['meeting_id'] + '\nСтатус: ' + event['status'] + '\nОткройте интерфейс для подробностей.\n')
        context = ssl.create_default_context()
        if mode == 'ssl':
            client = smtplib.SMTP_SSL(host, port, timeout=15, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=15)
        with client:
            if mode == 'starttls':
                client.starttls(context=context)
            if config.get('username'):
                client.login(config['username'], config.get('password', ''))
            client.send_message(msg)
