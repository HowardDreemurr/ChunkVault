"""Chunk-level semantic snapshot store.

This is the storage backend designed for the realistic scale problem:
hundreds of snapshots of a 100+ GB world, all needing to fit in <2 TB.

Where ``chunkvault.storage`` (git-based) stores whole region files as opaque
blobs and only deduplicates files that are byte-identical, ``chunkvault.store``
deduplicates at the **MC chunk** level. MC's per-chunk metadata fields
(LastUpdate etc.) flicker constantly and torch git's file-level dedup —
but the actual chunk content (raw compressed payload) is stable across
snapshots whenever a player isn't actively touching it. Hashing per-chunk
recovers an order-of-magnitude better dedup ratio.
"""
from .chunk_store import ChunkStore
from .importer import ImportError_ as ImportError, ImportSession, iter_archives
from .ingest import (
    IngestResult,
    collect_log_files,
    discover_servers,
    ingest_archive,
    parse_timestamp_from_name,
)
from .inspect import ArchivePreview, ArchiveServerPreview, preview_archive
from .progress import ProgressCallback, ProgressEvent
from .index import IndexDB, SnapshotRow
from .manifest import (
    ChunkRecord,
    FileRecord,
    Manifest,
    ManifestError,
    ManifestHeader,
    RegionRecord,
    read_manifest,
    write_manifest,
)
from .log_manifest import (
    LogFileRecord,
    LogManifest,
    LogManifestError,
    read_log_manifest,
    write_log_manifest,
)
from .log_store import LogStore
from .repo import (
    DEFAULT_EXCLUDE,
    ChunkRepoError,
    ChunkSnapshot,
    ChunkSnapshotRepo,
    FsckReport,
    GCResult,
    LogSnapshot,
    RoundTripVerificationError,
    VerifyReport,
)

__all__ = [
    "ChunkStore",
    "IndexDB",
    "SnapshotRow",
    "ChunkRecord",
    "RegionRecord",
    "FileRecord",
    "Manifest",
    "ManifestHeader",
    "ManifestError",
    "read_manifest",
    "write_manifest",
    "ChunkSnapshot",
    "ChunkSnapshotRepo",
    "VerifyReport",
    "GCResult",
    "FsckReport",
    "RoundTripVerificationError",
    "LogSnapshot",
    "LogStore",
    "LogFileRecord",
    "LogManifest",
    "LogManifestError",
    "read_log_manifest",
    "write_log_manifest",
    "ChunkRepoError",
    "DEFAULT_EXCLUDE",
    "ImportSession",
    "ImportError",
    "iter_archives",
    "ingest_archive",
    "IngestResult",
    "discover_servers",
    "collect_log_files",
    "parse_timestamp_from_name",
    "ProgressCallback",
    "ProgressEvent",
    "ArchivePreview",
    "ArchiveServerPreview",
    "preview_archive",
]
