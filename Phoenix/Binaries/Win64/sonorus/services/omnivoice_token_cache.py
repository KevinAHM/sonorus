"""Torch-free reader/writer for OmniVoice ``.tokens.pt`` sidecars.

The PyTorch and native OmniVoice providers share the same eight RVQ codebook
streams.  PyTorch stores them in its ZIP-based ``torch.save`` container, but
requiring torch merely to recover one contiguous int64 tensor would defeat the
native provider.  This module accepts only the narrow, known-safe serialization
shape written by ``services.omnivoice_engine`` and never imports torch.
"""
import collections
import io
import math
import os
import pickle
import struct
import uuid
import zipfile
from pathlib import Path
from typing import Any

import numpy as np


_NUM_CODEBOOKS = 8
_MAX_REFERENCE_FRAMES = 60 * 25
_MAX_CODE_VALUE = 1023


class _LongStorageType:
    pass


class _Storage:
    def __init__(self, values: np.ndarray):
        self.values = values


class _TokenCacheUnpickler(pickle.Unpickler):
    """Restricted unpickler for the exact tensor payload emitted by torch.save."""

    def __init__(self, payload: bytes, archive: zipfile.ZipFile, root: str):
        super().__init__(io.BytesIO(payload))
        self._archive = archive
        self._root = root

    def find_class(self, module: str, name: str) -> Any:
        if (module, name) == ("torch", "LongStorage"):
            return _LongStorageType
        if (module, name) == ("torch._utils", "_rebuild_tensor_v2"):
            return self._rebuild_tensor_v2
        if (module, name) == ("collections", "OrderedDict"):
            return collections.OrderedDict
        raise pickle.UnpicklingError(f"Unsupported token-cache global: {module}.{name}")

    def persistent_load(self, persistent_id: Any) -> _Storage:
        if (
            not isinstance(persistent_id, tuple)
            or len(persistent_id) != 5
            or persistent_id[0] != "storage"
            or persistent_id[1] is not _LongStorageType
            or not isinstance(persistent_id[2], str)
            or persistent_id[3] != "cpu"
            or not isinstance(persistent_id[4], int)
        ):
            raise pickle.UnpicklingError("Unsupported token-cache storage descriptor")

        key = persistent_id[2]
        element_count = persistent_id[4]
        if not key.isdigit() or not 0 < element_count <= _NUM_CODEBOOKS * _MAX_REFERENCE_FRAMES:
            raise pickle.UnpicklingError("Invalid token-cache storage dimensions")

        raw = self._archive.read(f"{self._root}/data/{key}")
        if len(raw) != element_count * np.dtype("<i8").itemsize:
            raise pickle.UnpicklingError("Token-cache storage has an invalid byte count")
        return _Storage(np.frombuffer(raw, dtype="<i8"))

    @staticmethod
    def _rebuild_tensor_v2(
        storage: _Storage,
        storage_offset: int,
        size: tuple,
        stride: tuple,
        requires_grad: bool,
        backward_hooks: Any,
    ) -> np.ndarray:
        del requires_grad, backward_hooks
        if (
            not isinstance(storage, _Storage)
            or storage_offset != 0
            or not isinstance(size, tuple)
            or len(size) != 2
            or size[0] != _NUM_CODEBOOKS
            or not isinstance(size[1], int)
            or not 0 < size[1] <= _MAX_REFERENCE_FRAMES
            or stride != (size[1], 1)
            or storage.values.size != size[0] * size[1]
        ):
            raise pickle.UnpicklingError("Unsupported token-cache tensor layout")
        return storage.values.reshape(size)


def load_omnivoice_token_cache(path: Path) -> dict[str, Any]:
    """Load the shared audio codes, RMS, and transcript without importing torch."""
    with zipfile.ZipFile(path) as archive:
        data_names = [name for name in archive.namelist() if name.endswith("/data.pkl")]
        if len(data_names) != 1:
            raise ValueError("Token cache does not contain exactly one data.pkl")
        data_name = data_names[0]
        root = data_name.rsplit("/", 1)[0]
        if archive.read(f"{root}/byteorder") != b"little":
            raise ValueError("Only little-endian OmniVoice token caches are supported")
        payload = _TokenCacheUnpickler(archive.read(data_name), archive, root).load()

    if not isinstance(payload, dict):
        raise ValueError("Token cache payload is not a dictionary")
    codes = payload.get("audio_codes")
    ref_rms = payload.get("ref_rms")
    ref_text = payload.get("ref_text")
    if (
        not isinstance(codes, np.ndarray)
        or codes.dtype != np.dtype("<i8")
        or codes.ndim != 2
        or codes.shape[0] != _NUM_CODEBOOKS
        or codes.size == 0
        or int(codes.min()) < 0
        or int(codes.max()) > _MAX_CODE_VALUE
        or not isinstance(ref_rms, (int, float))
        or isinstance(ref_rms, bool)
        or not math.isfinite(float(ref_rms))
        or float(ref_rms) < 0.0
        or not isinstance(ref_text, str)
    ):
        raise ValueError("Token cache payload has invalid OmniVoice fields")
    return {
        "audio_codes": np.ascontiguousarray(codes, dtype=np.int32),
        "ref_rms": float(ref_rms),
        "ref_text": ref_text,
    }


def _pickle_unicode(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return b"X" + struct.pack("<I", len(encoded)) + encoded


def _pickle_int(value: int) -> bytes:
    if not -(2 ** 31) <= value < 2 ** 31:
        raise ValueError("Token-cache integer is outside pickle BININT range")
    return b"J" + struct.pack("<i", value)


def _build_payload(codes: np.ndarray, ref_rms: float, ref_text: str) -> bytes:
    frames = int(codes.shape[1])
    parts = [
        b"\x80\x02}(",
        _pickle_unicode("audio_codes"),
        b"ctorch._utils\n_rebuild_tensor_v2\n(",
        b"(",
        _pickle_unicode("storage"),
        b"ctorch\nLongStorage\n",
        _pickle_unicode("0"),
        _pickle_unicode("cpu"),
        _pickle_int(int(codes.size)),
        b"tQ",
        _pickle_int(0),
        b"(", _pickle_int(_NUM_CODEBOOKS), _pickle_int(frames), b"t",
        b"(", _pickle_int(frames), _pickle_int(1), b"t",
        b"\x89ccollections\nOrderedDict\n)RtR",
        _pickle_unicode("ref_rms"), b"G", struct.pack(">d", float(ref_rms)),
        _pickle_unicode("ref_text"), _pickle_unicode(ref_text),
        b"u.",
    ]
    return b"".join(parts)


def save_omnivoice_token_cache(
    path: Path,
    audio_codes: np.ndarray,
    ref_rms: float,
    ref_text: str,
) -> None:
    """Atomically write a ``torch.load(weights_only=True)`` compatible sidecar."""
    codes = np.asarray(audio_codes)
    if (
        codes.ndim != 2
        or codes.shape[0] != _NUM_CODEBOOKS
        or not 0 < codes.shape[1] <= _MAX_REFERENCE_FRAMES
        or codes.size == 0
        or int(codes.min()) < 0
        or int(codes.max()) > _MAX_CODE_VALUE
        or isinstance(ref_rms, bool)
        or not isinstance(ref_rms, (int, float, np.integer, np.floating))
        or not math.isfinite(float(ref_rms))
        or float(ref_rms) < 0.0
        or not isinstance(ref_text, str)
    ):
        raise ValueError("Cannot save invalid OmniVoice audio codes")
    codes_i64 = np.ascontiguousarray(codes, dtype="<i8")

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    archive_root = destination.stem
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(f"{archive_root}/data.pkl", _build_payload(codes_i64, ref_rms, ref_text))
            archive.writestr(f"{archive_root}/byteorder", b"little")
            archive.writestr(f"{archive_root}/data/0", codes_i64.tobytes(order="C"))
            archive.writestr(f"{archive_root}/version", b"3\n")
            archive.writestr(f"{archive_root}/.format_version", b"1")
            archive.writestr(f"{archive_root}/.storage_alignment", b"64")
            archive.writestr(
                f"{archive_root}/.data/serialization_id",
                str(uuid.uuid4().int).encode("ascii"),
            )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
