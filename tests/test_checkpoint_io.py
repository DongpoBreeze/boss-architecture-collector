"""Offline checkpoint readers, including real Windows sharing semantics."""

import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import checkpoint_io
import jd_parallel
import process_architecture as processor


class CheckpointReaderTests(unittest.TestCase):
    def test_non_windows_reader_and_replacer_use_portable_file_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "checkpoint.json"
            temporary = Path(directory) / "new.json"
            target.write_text('{"generation": 0}', encoding="utf-8")
            temporary.write_text('{"generation": 1}', encoding="utf-8")
            with patch.object(checkpoint_io, "os", SimpleNamespace(name="posix", replace=os.replace)), \
                 patch.object(checkpoint_io, "_windows_file_api", side_effect=AssertionError("must not use Windows APIs")):
                self.assertEqual(checkpoint_io.read_json(target), {"generation": 0})
                checkpoint_io.replace_with_retry(temporary, target)
                self.assertEqual(checkpoint_io.read_json(target), {"generation": 1})

    def test_decode_and_json_parsing_happen_after_reader_closes(self):
        reader = io.BytesIO(b'\xef\xbb\xbf{"snapshot": true}')
        loads = json.loads

        def parse(value):
            self.assertTrue(reader.closed)
            return loads(value)

        with patch.object(checkpoint_io, "open_shared_read", return_value=reader), \
             patch.object(checkpoint_io.json, "loads", side_effect=parse):
            self.assertEqual(checkpoint_io.read_json("unused"), {"snapshot": True})

    def test_invalid_encoding_or_json_closes_the_reader_and_missing_file_stays_missing(self):
        for payload, expected in ((b"\xff", UnicodeDecodeError), (b"{", ValueError)):
            with self.subTest(payload=payload):
                reader = io.BytesIO(payload)
                with patch.object(checkpoint_io, "open_shared_read", return_value=reader), self.assertRaises(expected):
                    checkpoint_io.read_json("unused")
                self.assertTrue(reader.closed)
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with self.assertRaises(FileNotFoundError):
                checkpoint_io.read_json(missing)
            self.assertFalse(missing.exists())

    @unittest.skipUnless(os.name == "nt", "Windows delete sharing")
    def test_shared_reader_can_remain_open_across_atomic_replacement_and_keeps_its_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "正文检查点.json"
            jd_parallel._write_checkpoint(target, [{"generation": 0}])
            with checkpoint_io.open_shared_read(target) as held, \
                 patch.object(checkpoint_io.time, "sleep", side_effect=AssertionError("shared reader must not require retry")):
                jd_parallel._write_checkpoint(target, [{"generation": 1}])
                processor.write_json(target, [{"generation": 2}])
                self.assertEqual(json.loads(held.read()), [{"generation": 0}])
                self.assertEqual(checkpoint_io.read_json(target), [{"generation": 2}])
                self.assertFalse(held.closed)

    @unittest.skipUnless(os.name == "nt", "Windows delete sharing")
    def test_processor_and_adapter_recovery_readers_allow_replace_while_their_handle_is_open(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "details.json"
            open_read = checkpoint_io.open_shared_read
            for read in (processor.read_json, jd_parallel._read_checkpoint):
                with self.subTest(reader=read.__name__):
                    processor.write_json(target, [{"job_id": "saved", "jd": "旧的完整正文"}])

                    def open_and_publish(path):
                        handle = open_read(path)
                        try:
                            jd_parallel._write_checkpoint(path, [{"job_id": "saved", "jd": "新的完整正文"}])
                        except BaseException:
                            handle.close()
                            raise
                        return handle

                    with patch.object(checkpoint_io, "open_shared_read", side_effect=open_and_publish), \
                         patch.object(checkpoint_io.time, "sleep", side_effect=AssertionError("own reader blocked publication")):
                        self.assertEqual(read(target)[0]["jd"], "旧的完整正文")
                    self.assertEqual(checkpoint_io.read_json(target)[0]["jd"], "新的完整正文")

    @unittest.skipUnless(os.name == "nt", "Windows sharing lock")
    def test_real_external_reader_held_longer_than_old_retry_window_releases_and_write_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "details.json"
            processor.write_json(target, {"generation": 0})
            with target.open("rb") as exclusive:
                release = threading.Timer(.8, exclusive.close)
                release.start()
                started = time.monotonic()
                try:
                    jd_parallel._write_checkpoint(target, [{"generation": 1}])
                finally:
                    release.join(timeout=2)
            self.assertGreaterEqual(time.monotonic() - started, .75)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(checkpoint_io.read_json(target), [{"generation": 1}])

    @unittest.skipUnless(os.name == "nt", "Windows sharing lock")
    def test_stop_cancels_retry_for_real_external_lock_without_publishing_or_losing_old_json(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "details.json"
            processor.write_json(target, [{"generation": 0}])
            control = jd_parallel.StopState()
            with target.open("rb"):
                cancel = threading.Timer(.15, control.request_stop)
                cancel.start()
                started = time.monotonic()
                try:
                    with self.assertRaises(jd_parallel.CheckpointWriteError) as raised:
                        jd_parallel._write_checkpoint(target, [{"generation": 1}], control=control)
                finally:
                    cancel.join(timeout=1)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(raised.exception.operation, "replace")
            self.assertEqual(raised.exception.errno, errno.EACCES)
            self.assertEqual(checkpoint_io.read_json(target), [{"generation": 0}])
            self.assertEqual(list(target.parent.glob(".*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows cross-process sharing")
    def test_live_readers_and_real_writer_process_share_atomic_snapshots_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "details.json"
            processor.write_json(target, [{"generation": 0}])
            code = "\n".join((
                "import sys,time", "import jd_parallel as adapter", "import process_architecture as processor",
                "from unittest.mock import patch", "from pathlib import Path", "import checkpoint_io", "target=Path(sys.argv[1])",
                "print('ready',flush=True)", "sys.stdin.readline()",
                "with patch.object(checkpoint_io.time,'sleep',side_effect=AssertionError('reader blocked replace')):",
                "    for generation in range(1,31):",
                "        writer=adapter._write_checkpoint if generation%2 else processor.write_json",
                "        writer(target,[{'generation':generation,'jd':'完整正文'*100}])",
                "        print(generation,flush=True)",
            ))
            environment = dict(os.environ, PYTHONPATH=str(PROJECT), PYTHONUTF8="1")
            child = subprocess.Popen([sys.executable, "-u", "-c", code, str(target)], cwd=directory,
                                     env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, encoding="utf-8")
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                observations = []
                with checkpoint_io.open_shared_read(target) as held:
                    child.stdin.write("start\n")
                    child.stdin.flush()
                    for _ in range(30):
                        observations.append(checkpoint_io.read_json(target)[0]["generation"])
                        self.assertTrue(child.stdout.readline().strip().isdigit())
                    self.assertFalse(held.closed)
                    self.assertEqual(json.loads(held.read()), [{"generation": 0}])
                child.stdin.close()
                self.assertEqual(child.wait(timeout=5), 0, child.stderr.read())
                self.assertEqual(len(observations), 30)
                self.assertEqual(checkpoint_io.read_json(target)[0]["generation"], 30)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream:
                        stream.close()


if __name__ == "__main__":
    unittest.main()
