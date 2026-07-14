"""
Pure-Python package ID scan for cooked Unreal IoStore mods.

This mirrors the core idea of PakChunkChecker.bat + UEcastoc:
- find .utoc containers
- derive each mod's package ID
- group duplicate package IDs as chunk conflicts

The most faithful source is the dependencies blob stored in the matching .ucas.
UEcastoc's manifest writer reads `Dependencies.packageID` from that blob.

As a safety fallback, if the dependencies blob cannot be decoded here
(for example an unsupported compression method), we fall back to the
ContainerID stored in the .utoc header. In UEcastoc's packer, those IDs
are written from the same dependency/package-store chunk, so they should
match for normal cooked mods.
"""

from __future__ import annotations

import argparse
import json
import struct
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


UTOC_HEADER_SIZE = 144
UTOC_MAGIC = b"-==--==--==--==-"
CHUNK_ID_SIZE = 12
OFFSET_LENGTH_SIZE = 10
COMPRESSION_BLOCK_SIZE = 12

CHUNK_TYPE_PACKAGE_STORE_ENTRY = 10


class PackageIdCheckError(RuntimeError):
    """Raised when a container cannot be parsed."""


@dataclass
class UtocHeader:
    version: int
    header_size: int
    entry_count: int
    compressed_block_entry_count: int
    compressed_block_entry_size: int
    compression_method_name_count: int
    compression_method_name_length: int
    compression_block_size: int
    directory_index_size: int
    partition_count: int
    container_id: int
    container_flags: int
    perfect_hash_seed_count: int
    toc_chunks_without_perfect_hash_count: int


@dataclass
class IoChunkId:
    chunk_id: int
    index: int
    padding: int
    chunk_type: int


@dataclass
class OffsetAndLength:
    offset: int
    length: int


@dataclass
class CompressionBlock:
    offset: int
    compressed_size: int
    uncompressed_size: int
    compression_method: int


@dataclass
class PackageIdEntry:
    name: str
    utoc_path: str
    ucas_path: str | None
    package_id: int
    package_id_hex: str
    source: str


def _parse_utoc_header(data: bytes) -> UtocHeader:
    if len(data) < UTOC_HEADER_SIZE:
        raise PackageIdCheckError("utoc header is truncated")
    if data[:16] != UTOC_MAGIC:
        raise PackageIdCheckError("utoc magic does not match")

    return UtocHeader(
        version=data[16],
        header_size=struct.unpack_from("<I", data, 20)[0],
        entry_count=struct.unpack_from("<I", data, 24)[0],
        compressed_block_entry_count=struct.unpack_from("<I", data, 28)[0],
        compressed_block_entry_size=struct.unpack_from("<I", data, 32)[0],
        compression_method_name_count=struct.unpack_from("<I", data, 36)[0],
        compression_method_name_length=struct.unpack_from("<I", data, 40)[0],
        compression_block_size=struct.unpack_from("<I", data, 44)[0],
        directory_index_size=struct.unpack_from("<I", data, 48)[0],
        partition_count=struct.unpack_from("<I", data, 52)[0],
        container_id=struct.unpack_from("<Q", data, 56)[0],
        container_flags=data[80],
        perfect_hash_seed_count=struct.unpack_from("<I", data, 84)[0],
        toc_chunks_without_perfect_hash_count=struct.unpack_from("<I", data, 96)[0],
    )


def _read_5_byte_uint(raw: bytes) -> int:
    if len(raw) != 5:
        raise ValueError("expected 5 bytes")
    return (
        raw[4]
        | (raw[3] << 8)
        | (raw[2] << 16)
        | (raw[1] << 24)
        | (raw[0] << 32)
    )


def _read_3_byte_uint(raw: bytes) -> int:
    if len(raw) != 3:
        raise ValueError("expected 3 bytes")
    return raw[0] | (raw[1] << 8) | (raw[2] << 16)


def _parse_chunk_ids(data: bytes, start: int, count: int) -> tuple[list[IoChunkId], int]:
    chunk_ids: list[IoChunkId] = []
    cursor = start
    for _ in range(count):
        chunk_id, index, padding, chunk_type = struct.unpack_from("<QHBB", data, cursor)
        chunk_ids.append(IoChunkId(chunk_id=chunk_id, index=index, padding=padding, chunk_type=chunk_type))
        cursor += CHUNK_ID_SIZE
    return chunk_ids, cursor


def _parse_offsets_and_lengths(data: bytes, start: int, count: int) -> tuple[list[OffsetAndLength], int]:
    entries: list[OffsetAndLength] = []
    cursor = start
    for _ in range(count):
        offset = _read_5_byte_uint(data[cursor : cursor + 5])
        length = _read_5_byte_uint(data[cursor + 5 : cursor + 10])
        entries.append(OffsetAndLength(offset=offset, length=length))
        cursor += OFFSET_LENGTH_SIZE
    return entries, cursor


def _parse_compression_blocks(data: bytes, start: int, count: int) -> tuple[list[CompressionBlock], int]:
    blocks: list[CompressionBlock] = []
    cursor = start
    for _ in range(count):
        offset = int.from_bytes(data[cursor : cursor + 5], "little")
        compressed_size = _read_3_byte_uint(data[cursor + 5 : cursor + 8])
        uncompressed_size = _read_3_byte_uint(data[cursor + 8 : cursor + 11])
        compression_method = data[cursor + 11]
        blocks.append(
            CompressionBlock(
                offset=offset,
                compressed_size=compressed_size,
                uncompressed_size=uncompressed_size,
                compression_method=compression_method,
            )
        )
        cursor += COMPRESSION_BLOCK_SIZE
    return blocks, cursor


def _parse_compression_methods(data: bytes, start: int, count: int, name_length: int) -> tuple[list[str], int]:
    methods = ["None"]
    cursor = start
    for _ in range(count):
        raw = data[cursor : cursor + name_length]
        methods.append(raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore"))
        cursor += name_length
    return methods, cursor


def _decompress_block(raw: bytes, method: str, expected_size: int) -> bytes:
    method_normalized = (method or "None").lower()
    if method_normalized in {"", "none"}:
        return raw
    if method_normalized == "zlib":
        return zlib.decompress(raw)
    raise PackageIdCheckError(f"unsupported compression method for dependency decode: {method}")


def _find_dependency_entry_index(chunk_ids: Iterable[IoChunkId], container_id: int) -> int:
    for index, chunk in enumerate(chunk_ids):
        if chunk.chunk_type == CHUNK_TYPE_PACKAGE_STORE_ENTRY:
            return index
    for index, chunk in enumerate(chunk_ids):
        if chunk.chunk_id == container_id:
            return index
    raise PackageIdCheckError("could not find dependency/package-store chunk")


def read_package_id(utoc_path: str | Path) -> PackageIdEntry:
    utoc = Path(utoc_path)
    ucas = utoc.with_suffix(".ucas")

    data = utoc.read_bytes()
    header = _parse_utoc_header(data)

    cursor = header.header_size
    chunk_ids, cursor = _parse_chunk_ids(data, cursor, header.entry_count)
    offsets_and_lengths, cursor = _parse_offsets_and_lengths(data, cursor, header.entry_count)

    cursor += header.perfect_hash_seed_count * 4
    cursor += header.toc_chunks_without_perfect_hash_count * 4

    blocks, cursor = _parse_compression_blocks(data, cursor, header.compressed_block_entry_count)
    methods, cursor = _parse_compression_methods(
        data,
        cursor,
        header.compression_method_name_count,
        header.compression_method_name_length,
    )

    try:
        if not ucas.exists():
            raise PackageIdCheckError("matching ucas file is missing")

        dependency_index = _find_dependency_entry_index(chunk_ids, header.container_id)
        dependency_entry = offsets_and_lengths[dependency_index]

        start_block = dependency_entry.offset // header.compression_block_size
        block_count = (dependency_entry.length + header.compression_block_size - 1) // header.compression_block_size
        end_block = start_block + block_count
        dependency_blocks = blocks[start_block:end_block]

        reconstructed = bytearray()
        with ucas.open("rb") as f:
            for block in dependency_blocks:
                f.seek(block.offset)
                raw = f.read(block.compressed_size)
                method_name = methods[block.compression_method] if block.compression_method < len(methods) else "None"
                reconstructed.extend(_decompress_block(raw, method_name, block.uncompressed_size))

        if len(reconstructed) < 8:
            raise PackageIdCheckError("dependency blob is too small to contain package id")

        package_id = struct.unpack_from("<Q", reconstructed, 0)[0]
        source = "ucas_dependencies"
    except Exception:
        package_id = header.container_id
        source = "utoc_header_fallback"

    return PackageIdEntry(
        name=utoc.stem,
        utoc_path=str(utoc.resolve()),
        ucas_path=str(ucas.resolve()) if ucas.exists() else None,
        package_id=package_id,
        package_id_hex=f"0x{package_id:016x}",
        source=source,
    )


def scan_package_ids(root: str | Path) -> dict:
    root_path = Path(root)
    entries = [read_package_id(path) for path in sorted(root_path.rglob("*.utoc"))]

    grouped: dict[int, list[PackageIdEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.package_id, []).append(entry)

    conflicts = {
        f"0x{package_id:016x}": [asdict(entry) for entry in group]
        for package_id, group in sorted(grouped.items(), key=lambda item: item[0])
        if len(group) > 1
    }

    return {
        "root": str(root_path.resolve()),
        "entries": [asdict(entry) for entry in sorted(entries, key=lambda entry: (entry.package_id, entry.name.lower()))],
        "conflicts": conflicts,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan cooked IoStore containers for duplicate package IDs.")
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Folder to scan recursively for .utoc files",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print full JSON output instead of a compact text report",
    )
    return parser


def _print_text_report(result: dict) -> None:
    entries = result["entries"]
    conflicts = result["conflicts"]

    print(f"Scanned {len(entries)} cooked container(s) under {result['root']}")
    if not entries:
        return

    for entry in entries:
        print(
            f"{entry['package_id_hex']}  {entry['name']}  source={entry['source']}"
        )

    if not conflicts:
        print("\nNo duplicate package IDs found.")
        return

    print("\nConflicts:")
    for package_id_hex, group in conflicts.items():
        names = ", ".join(entry["name"] for entry in group)
        print(f"{package_id_hex}: {names}")


def main() -> int:
    args = _build_arg_parser().parse_args()
    result = scan_package_ids(args.root)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_text_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
