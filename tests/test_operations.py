import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from operations import Operations


class DummyJobs:
    def __init__(self, root):
        self.output = root / 'output'; self.output.mkdir()
        self.state_dir = root / 'state'; self.state_dir.mkdir()
        self.condition = threading.Condition()
        self.settings = {'resolution': '1280x800'}
        self.jobs = {'1'*32: {'id':'1'*32, 'meeting_id':'a'*40+'-1234567890123', 'status':'done'}}


class OperationsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = DummyJobs(Path(self.temp.name))
        self.ops = Operations(self.jobs)

    def config(self, data):
        path = self.jobs.state_dir / 'operations.json'
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def test_must_be_private_and_backup_requires_mounted_external_volume(self):
        self.config({'backup_dir': self.temp.name})
        (self.jobs.state_dir / 'operations.json').chmod(0o644)
        self.assertIn('Ошибка конфигурации', self.ops.health()['backup_status'])
        with self.assertRaises(ValueError): self.ops.backup()
        (self.jobs.state_dir / 'operations.json').chmod(0o600)
        with mock.patch('operations.os.path.ismount', return_value=True):
            with self.assertRaisesRegex(ValueError, 'другой файловой системе'):
                self.ops.backup()

    @unittest.skipUnless(Path('/dev/shm').is_dir(), 'need a second filesystem for backup test')
    def test_backup_copies_completed_result_and_state_without_credentials(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm') as backup:
            if Path(backup).stat().st_dev == self.jobs.output.stat().st_dev:
                self.skipTest('/dev/shm is on the same filesystem')
            self.config({'backup_dir': backup, 'smtp': {'password':'SECRET'}})
            job = next(iter(self.jobs.jobs.values()))
            name = job['meeting_id'] + '.mp4'
            (self.jobs.output / name).write_bytes(b'completed video')
            with mock.patch('operations.os.path.ismount', return_value=True):
                self.ops.backup()
                dest = Path(backup) / 'bbb-mp4-simple'
                self.assertEqual((dest / 'recordings' / name).read_bytes(), b'completed video')
                self.assertEqual(json.loads((dest / 'state/jobs.json').read_text())[0]['status'], 'done')
                self.assertFalse((dest / 'state/operations.json').exists())
                # Removing a local result doesn't remove its independent backup.
                (self.jobs.output / name).unlink()
                job['status'] = 'failed'
                self.ops.backup()
                self.assertEqual((dest / 'recordings' / name).read_bytes(), b'completed video')

    def test_smtp_uses_tls_and_does_not_include_url(self):
        self.config({'smtp': {'host':'smtp.example.test', 'port':465, 'mode':'ssl',
                              'from':'a@example.test', 'to':'b@example.test',
                              'username':'user', 'password':'secret'}})
        with mock.patch('operations.smtplib.SMTP_SSL') as smtp:
            client = smtp.return_value
            self.ops.notify({'meeting_id':'a'*40+'-1234567890123','status':'done', 'url':'https://secret/token'})
            client.login.assert_called_once_with('user','secret')
            message = client.send_message.call_args.args[0]
            self.assertNotIn('https://secret/token', message.as_string())
            self.assertNotIn('secret', message.as_string())


if __name__ == '__main__': unittest.main()
