import struct
import tempfile
import unittest
from pathlib import Path
from web import mp4_duration


def box(kind, payload, extended=False):
    if extended:
        return struct.pack('>I4sQ', 1, kind, 16 + len(payload)) + payload
    return struct.pack('>I4s', 8 + len(payload), kind) + payload


class MetadataTest(unittest.TestCase):
    def test_versions_and_extended_box_sizes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.mp4'
            for version in (0, 1):
                if version == 0:
                    mvhd = b'\0\0\0\0' + struct.pack('>IIII', 0, 0, 1000, 51750)
                else:
                    mvhd = b'\1\0\0\0' + struct.pack('>QQIQ', 0, 0, 1000, 51750)
                path.write_bytes(box(b'ftyp', b'isom') + box(b'mdat', b'video') + box(b'moov', box(b'mvhd', mvhd), extended=True))
                self.assertEqual(mp4_duration(path), 51.75)
            path.write_bytes(b'bad file')
            self.assertIsNone(mp4_duration(path))


if __name__ == '__main__':
    unittest.main()
