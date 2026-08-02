import hashlib
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError

SONORUS_ROOT = Path(__file__).resolve().parents[1]
if str(SONORUS_ROOT) not in sys.path:
    sys.path.insert(0, str(SONORUS_ROOT))

from services import omnivoice_cpp_engine as engine


class FakeProcess:
    def __init__(self, alive=True):
        self.alive = alive
        self.terminated = 0
        self.killed = 0
        self.joined = 0
        self.closed = False

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated += 1
        self.alive = False

    def kill(self):
        self.killed += 1
        self.alive = False

    def join(self, timeout=None):
        self.joined += 1

    def close(self):
        self.closed = True


class FakeQueue:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []
        self.closed = False
        self.cancelled = False

    def put(self, value):
        self.requests.append(value)

    def put_nowait(self, value):
        self.requests.append(value)

    def get(self, timeout=None):
        if self.responses:
            return self.responses.pop(0)
        raise queue.Empty

    def cancel_join_thread(self):
        self.cancelled = True

    def close(self):
        self.closed = True


class WorkerInvalidationTests(unittest.TestCase):
    def _manager_with_worker(self):
        manager = engine.OmniVoiceCppProcessManager()
        process = FakeProcess()
        request_queue = FakeQueue()
        response_queue = FakeQueue()
        manager._process = process
        manager._request_queue = request_queue
        manager._response_queue = response_queue
        manager._ready = True
        return manager, process, request_queue, response_queue

    def test_timeout_force_discards_worker_and_queues(self):
        manager, process, request_queue, response_queue = self._manager_with_worker()

        self.assertIsNone(manager._await_response(process, response_queue, timeout=0.0))

        self.assertEqual(process.terminated, 1)
        self.assertGreaterEqual(process.joined, 1)
        self.assertTrue(process.closed)
        self.assertTrue(request_queue.closed)
        self.assertTrue(response_queue.closed)
        self.assertIsNone(manager._process)
        self.assertIsNone(manager._request_queue)
        self.assertIsNone(manager._response_queue)
        self.assertFalse(manager._ready)

    def test_worker_death_without_final_response_discards_handles(self):
        manager, process, request_queue, response_queue = self._manager_with_worker()
        process.alive = False

        self.assertIsNone(manager._await_response(process, response_queue, timeout=1.0))

        self.assertEqual(process.terminated, 0)
        self.assertGreaterEqual(process.joined, 1)
        self.assertTrue(request_queue.closed)
        self.assertTrue(response_queue.closed)
        self.assertIsNone(manager._process)
        self.assertFalse(manager._ready)

    def test_request_wait_is_pinned_to_captured_worker_generation(self):
        manager, old_process, old_requests, old_responses = self._manager_with_worker()
        old_responses.responses.append({"type": "old_done"})
        handles = manager._submit_request({"type": "warmup"})

        new_process = FakeProcess()
        new_requests = FakeQueue()
        new_responses = FakeQueue([{"type": "new_done"}])
        manager._process = new_process
        manager._request_queue = new_requests
        manager._response_queue = new_responses
        manager._ready = True

        self.assertEqual(handles, (old_process, old_responses))
        self.assertEqual(old_requests.requests, [{"type": "warmup"}])
        self.assertEqual(manager._await_response(*handles, timeout=1.0), {"type": "old_done"})
        self.assertEqual(new_responses.responses, [{"type": "new_done"}])

        self.assertIsNone(manager._await_response(*handles, timeout=0.0))
        self.assertEqual(new_process.terminated, 0)
        self.assertIs(manager._process, new_process)
        self.assertIs(manager._response_queue, new_responses)

    def test_every_request_kind_invalidates_on_timeout(self):
        cases = (
            ("synthesize", 300.0),
            ("pretokenize", 120.0),
            ("warmup", 120.0),
            ("clear", 5.0),
        )
        for operation, timeout in cases:
            with self.subTest(operation=operation):
                manager, process, request_queue, response_queue = self._manager_with_worker()
                manager.ensure_started = mock.Mock(return_value=True)
                monotonic = mock.Mock(side_effect=[0.0, timeout + 1.0])
                with mock.patch.object(engine.time, "monotonic", monotonic), \
                     mock.patch.object(engine, "ensure_voice_reference_transcript", return_value=None):
                    if operation == "synthesize":
                        result = manager.synthesize_sentence("hello", "voice.wav", mock.Mock())
                        self.assertEqual(result, (False, 0))
                    elif operation == "pretokenize":
                        self.assertFalse(manager.pretokenize_voice("voice.wav"))
                    elif operation == "warmup":
                        self.assertIsNone(manager.warm_up("voice.wav"))
                    else:
                        self.assertIsNone(manager.clear_voice_prompt("voice.wav"))

                self.assertEqual(process.terminated, 1)
                self.assertTrue(request_queue.closed)
                self.assertTrue(response_queue.closed)
                self.assertFalse(manager._ready)


class AbiProbeTests(unittest.TestCase):
    def setUp(self):
        engine._runtime_abi_cache.clear()

    def _run_probe(self, result, identity=("runtime-a",)):
        with mock.patch.object(engine, "_runtime_dll_identity", return_value=identity), \
             mock.patch.object(engine.subprocess, "run", return_value=result) as run:
            error = engine._probe_runtime_abi(Path("C:/runtime/omnivoice.dll"))
        return error, run

    def test_probe_passes_when_both_defaults_report_abi_four(self):
        result = subprocess.CompletedProcess([], 0, '{"init_abi": 4, "tts_abi": 4}\n', "")
        error, _ = self._run_probe(result)
        self.assertIsNone(error)

    def test_probe_reports_incompatible_or_missing_export(self):
        mismatch = subprocess.CompletedProcess([], 0, '{"init_abi": 4, "tts_abi": 3}\n', "")
        error, _ = self._run_probe(mismatch, ("mismatch",))
        self.assertIn("incompatible", error)
        self.assertIn("tts=3", error)

        missing = subprocess.CompletedProcess(
            [], 2, '{"error": "missing required export ov_tts_default_params_v4"}\n', ""
        )
        error, _ = self._run_probe(missing, ("missing",))
        self.assertIn("ov_tts_default_params_v4", error)

    def test_probe_surfaces_child_crash(self):
        crashed = subprocess.CompletedProcess([], -1073741819, "", "access violation")
        error, _ = self._run_probe(crashed, ("crash",))
        self.assertIn("exit code -1073741819", error)
        self.assertIn("access violation", error)

    def test_transient_probe_failures_are_retried(self):
        success = subprocess.CompletedProcess([], 0, '{"init_abi": 4, "tts_abi": 4}\n', "")
        transient_failures = (
            subprocess.TimeoutExpired(["python", "-c"], 30),
            OSError("could not launch child"),
            subprocess.CompletedProcess([], -1073741819, "", "access violation"),
            subprocess.CompletedProcess([], 0, "unparseable output", ""),
        )
        for index, transient in enumerate(transient_failures):
            with self.subTest(transient=type(transient).__name__, index=index):
                engine._runtime_abi_cache.clear()
                with mock.patch.object(
                    engine, "_runtime_dll_identity", return_value=("same-runtime",)
                ), mock.patch.object(
                    engine.subprocess, "run", side_effect=[transient, success]
                ) as run:
                    self.assertIsNotNone(engine._probe_runtime_abi(Path("omnivoice.dll")))
                    self.assertIsNone(engine._probe_runtime_abi(Path("omnivoice.dll")))
                    self.assertIsNone(engine._probe_runtime_abi(Path("omnivoice.dll")))
                self.assertEqual(run.call_count, 2)

    def test_probe_cache_invalidates_when_any_runtime_identity_changes(self):
        result = subprocess.CompletedProcess([], 0, '{"init_abi": 4, "tts_abi": 4}\n', "")
        with mock.patch.object(
            engine, "_runtime_dll_identity", side_effect=[("a",), ("a",), ("b",)]
        ), mock.patch.object(engine.subprocess, "run", return_value=result) as run:
            self.assertIsNone(engine._probe_runtime_abi(Path("omnivoice.dll")))
            self.assertIsNone(engine._probe_runtime_abi(Path("omnivoice.dll")))
            self.assertIsNone(engine._probe_runtime_abi(Path("omnivoice.dll")))
        self.assertEqual(run.call_count, 2)

    def test_runtime_status_contains_meaningful_probe_error(self):
        dll_path = Path("C:/runtime/omnivoice.dll")
        with mock.patch.object(engine, "_is_valid_runtime_dll", return_value=True), \
             mock.patch.object(engine, "_find_dll", return_value=dll_path), \
             mock.patch.object(
                 engine, "_probe_runtime_abi",
                 return_value="missing required export ov_tts_default_params_v4",
             ):
            missing = engine.missing_runtime_files()
            self.assertFalse(engine.runtime_present())
        self.assertEqual(len(missing), 1)
        self.assertIn("ov_tts_default_params_v4", missing[0])


class FakeDownloadResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.headers = {"Content-Length": str(len(body))}
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def getcode(self):
        return self.status

    def read(self, size):
        if self.offset >= len(self.body):
            return b""
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


class Download416Tests(unittest.TestCase):
    def setUp(self):
        engine._upscaler_validation_cache.clear()

    def _integrity_patch(self, payload):
        return mock.patch.multiple(
            engine,
            UPSCALER_EXPECTED_BYTES=len(payload),
            UPSCALER_SHA256=hashlib.sha256(payload).hexdigest(),
        )

    def test_416_promotes_complete_valid_partial(self):
        payload = b"complete upscaler"
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / engine.UPSCALER_FILENAME
            partial = destination.with_name(destination.name + ".incomplete")
            partial.write_bytes(payload)
            error = HTTPError("https://example/model", 416, "range", None, None)

            with self._integrity_patch(payload), \
                 mock.patch("urllib.request.urlopen", side_effect=error) as urlopen:
                engine._download_upscaler_url("https://example/model", destination)

            self.assertEqual(destination.read_bytes(), payload)
            self.assertFalse(partial.exists())
            self.assertEqual(urlopen.call_count, 1)

    def test_416_deletes_invalid_partial_and_retries(self):
        payload = b"fresh complete upscaler"
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / engine.UPSCALER_FILENAME
            partial = destination.with_name(destination.name + ".incomplete")
            partial.write_bytes(b"bad")
            error = HTTPError("https://example/model", 416, "range", None, None)
            response = FakeDownloadResponse(payload)

            with self._integrity_patch(payload), \
                 mock.patch("urllib.request.urlopen", side_effect=[error, response]) as urlopen:
                engine._download_upscaler_url("https://example/model", destination)

            self.assertEqual(destination.read_bytes(), payload)
            self.assertFalse(partial.exists())
            self.assertEqual(urlopen.call_count, 2)
            first_request = urlopen.call_args_list[0].args[0]
            second_request = urlopen.call_args_list[1].args[0]
            self.assertIn("Range", first_request.headers)
            self.assertNotIn("Range", second_request.headers)


class UpscalerValidationTests(unittest.TestCase):
    def setUp(self):
        engine._upscaler_validation_cache.clear()

    def test_concurrent_cold_validation_hashes_file_once(self):
        payload = b"validated payload" * 1024
        expected_hash = hashlib.sha256(payload).hexdigest()
        real_sha256 = hashlib.sha256
        count = 0
        count_lock = threading.Lock()

        def counted_sha256():
            nonlocal count
            with count_lock:
                count += 1
            time.sleep(0.03)
            return real_sha256()

        with tempfile.TemporaryDirectory() as temp_dir:
            model = Path(temp_dir) / engine.UPSCALER_FILENAME
            model.write_bytes(payload)
            with mock.patch.multiple(
                engine,
                UPSCALER_EXPECTED_BYTES=len(payload),
                UPSCALER_SHA256=expected_hash,
            ), mock.patch.object(engine.hashlib, "sha256", side_effect=counted_sha256):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(lambda _: engine._is_valid_upscaler_model(model), range(8)))

        self.assertEqual(results, [True] * 8)
        self.assertEqual(count, 1)

    def test_changed_file_identity_is_not_cached_after_hash(self):
        payload = b"identity changes"
        with tempfile.TemporaryDirectory() as temp_dir:
            model = Path(temp_dir) / engine.UPSCALER_FILENAME
            model.write_bytes(payload)
            identity_a = (str(model.resolve()), len(payload), 1, 1)
            identity_b = (str(model.resolve()), len(payload), 2, 2)
            with mock.patch.multiple(
                engine,
                UPSCALER_EXPECTED_BYTES=len(payload),
                UPSCALER_SHA256=hashlib.sha256(payload).hexdigest(),
            ), mock.patch.object(
                engine, "_upscaler_file_identity", side_effect=[identity_a, identity_b]
            ):
                self.assertFalse(engine._is_valid_upscaler_model(model))
        self.assertEqual(engine._upscaler_validation_cache, {})


if __name__ == "__main__":
    unittest.main()
