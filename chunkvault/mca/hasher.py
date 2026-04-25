"""Content fingerprint for a chunk's on-disk representation.

We hash `(compression_scheme, payload)`. Two chunks that write to disk as
identical bytes hash the same; any difference — including a re-encoded NBT
tree with different zlib output — hashes differently. That's intentional:
false positives ("changed when nothing really changed") are acceptable for a
backup tool; false negatives would be catastrophic.

The external-storage flag (0x80) is masked out of the hash so that the same
logical content hashes identically whether it's stored inline in the .mca or
externally in a c.X.Z.mcc file. When hashing an external chunk, callers
should pass the .mcc bytes via `external_payload`, or use `hash_chunk_on_disk`
which does that lookup automatically.
"""
from __future__ import annotations

import hashlib

from .region import EXTERNAL_FLAG, RawChunk, Region

HASH_BYTES = 16  # blake2b-128


def hash_chunk(chunk: RawChunk, *, external_payload: bytes | None = None) -> bytes:
    """Content hash for a chunk.

    For an external chunk, `external_payload` should be the contents of the
    backing `c.X.Z.mcc` file. If omitted, only the stub is hashed — this
    will miss any changes to the external file, so use `hash_chunk_on_disk`
    when you have a `Region` in hand.
    """
    h = hashlib.blake2b(digest_size=HASH_BYTES)
    h.update(bytes([chunk.compression & ~EXTERNAL_FLAG]))
    if chunk.external and external_payload is not None:
        h.update(external_payload)
    else:
        h.update(chunk.payload)
    return h.digest()


def external_chunk_path(region: Region, chunk: RawChunk):
    """Absolute path to the c.X.Z.mcc file backing an external chunk.

    X/Z are world-space chunk coordinates: `region.rx * 32 + chunk.cx` etc.
    Raises ValueError if the region was built from ``Region.from_bytes``
    (no filesystem location to resolve relative to).
    """
    if region.path is None:
        raise ValueError(
            "Region has no filesystem path — external chunks cannot be resolved "
            "automatically. Use hash_chunk(chunk, external_payload=mcc_bytes)."
        )
    world_cx = region.coords.rx * 32 + chunk.cx
    world_cz = region.coords.rz * 32 + chunk.cz
    return region.path.parent / f"c.{world_cx}.{world_cz}.mcc"


def hash_chunk_on_disk(region: Region, chunk: RawChunk) -> bytes:
    """Hash a chunk, auto-reading the .mcc file when it's external.

    Missing .mcc files are treated as empty — hash is still stable but will
    not distinguish two orphaned stubs. That's corruption and should be
    surfaced by a validation pass, not by the hasher.

    For Regions constructed via ``from_bytes`` (no filesystem path), pass
    the external payload explicitly via ``hash_chunk(...)`` instead.
    """
    if not chunk.external:
        return hash_chunk(chunk)
    mcc_path = external_chunk_path(region, chunk)
    payload = mcc_path.read_bytes() if mcc_path.exists() else b""
    return hash_chunk(chunk, external_payload=payload)
