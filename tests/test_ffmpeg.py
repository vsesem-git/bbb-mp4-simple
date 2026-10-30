import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class FfmpegScriptTest(unittest.TestCase):
    def test_encoding_arguments_and_failure_propagation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ffmpeg = root / 'ffmpeg'
            ffmpeg.write_text('''#!/usr/bin/env python3
import json, os, sys
open(os.environ['ARG_LOG'], 'w').write(json.dumps(sys.argv[1:]))
sys.exit(int(os.environ.get('FFMPEG_EXIT', '0')))
''')
            ffmpeg.chmod(0o755)
            mv = root / 'mv'
            mv.write_text('#!/bin/sh\nprintf moved > "$MOVE_LOG"\n')
            mv.chmod(0o755)
            env = {**os.environ, 'PATH': str(root) + ':' + os.environ['PATH'],
                   'ARG_LOG': str(root / 'args.json'), 'MOVE_LOG': str(root / 'moved'),
                   'BBB_MP4_SIZE': '1280x720', 'BBB_MP4_VIDEO_BITRATE': '4000',
                   'BBB_MP4_AUDIO_BITRATE': '192', 'BBB_MP4_PRESET': 'slow'}
            cmd = ['sh', str(ROOT / 'ffmpeg-cmd.sh'), '51.737', 'abc', '128']
            subprocess.run(cmd, env=env, check=True)
            args = json.loads((root / 'args.json').read_text())
            self.assertEqual(args[args.index('-framerate')+1], '30')
            self.assertEqual(args[args.index('-c:v')+1], 'libx264')
            self.assertEqual(args[args.index('-b:v')+1], '4000k')
            self.assertEqual(args[args.index('-b:a')+1], '192k')
            self.assertEqual(args[args.index('-preset')+1], 'slow')
            self.assertEqual(args[args.index('-s')+1], '1280x720')
            self.assertEqual(args[args.index('-vf')+1], 'setsar=1')
            queues = [args[i+1] for i, value in enumerate(args) if value == '-thread_queue_size']
            self.assertEqual(queues, ['8', '128'])
            self.assertTrue((root / 'moved').exists())
            (root / 'moved').unlink()
            subprocess.run(cmd, env={**env, 'BBB_MP4_CODEC':'h264', 'BBB_MP4_VIDEO_BITRATE':'auto'}, check=True)
            args = json.loads((root / 'args.json').read_text())
            self.assertEqual(args[args.index('-c:v')+1], 'libx264')
            self.assertEqual(args[args.index('-crf')+1], '22')
            self.assertNotIn('-b:v', args)
            (root / 'moved').unlink()
            subprocess.run(cmd, env={**env, 'BBB_MP4_SIZE':'3440x1440', 'BBB_MP4_FPS':'60', 'BBB_MP4_CODEC':'h265', 'BBB_MP4_VIDEO_BITRATE':'auto'}, check=True)
            args = json.loads((root / 'args.json').read_text())
            self.assertEqual(args[args.index('-framerate')+1], '60')
            self.assertEqual(args[args.index('-s')+1], '3440x1440')
            self.assertEqual(args[args.index('-vf')+1], 'setsar=1')
            self.assertEqual(args[args.index('-c:v')+1], 'libx265')
            self.assertEqual(args[args.index('-crf')+1], '26')
            self.assertEqual(args[args.index('-tag:v')+1], 'hvc1')
            self.assertEqual(args[args.index('-x265-params')+1], 'log-level=error')
            self.assertNotIn('-b:v', args)
            (root / 'moved').unlink()
            subprocess.run(cmd, env={**env, 'BBB_MP4_CODEC':'h265'}, check=True)
            args = json.loads((root / 'args.json').read_text())
            self.assertEqual(args[args.index('-b:v')+1], '4000k')
            self.assertNotIn('-crf', args)
            for size in ('640x360','854x480','1280x720','1280x800','1920x1080','2560x1440','2560x1080','3440x1440'):
                subprocess.run(cmd, env={**env, 'BBB_MP4_SIZE':size}, check=True)
                args = json.loads((root / 'args.json').read_text())
                self.assertEqual(args[args.index('-s')+1], size)
                self.assertEqual(args[args.index('-vf')+1], 'setsar=1')
            result = subprocess.run(cmd, env={**env, 'BBB_MP4_SIZE':'999x999'})
            self.assertNotEqual(result.returncode, 0)
            (root / 'moved').unlink()
            result = subprocess.run(cmd, env={**env, 'FFMPEG_EXIT':'7'})
            self.assertEqual(result.returncode, 7)
            self.assertFalse((root / 'moved').exists())


if __name__ == '__main__':
    unittest.main()
