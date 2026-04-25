"""Ingest core tests — multi-server zip → snapshots + log snapshot."""
from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chunkvault.store import ChunkSnapshotRepo
from chunkvault.store.importer import ImportError_ as ImportError
from chunkvault.store.ingest import (
    collect_log_files,
    discover_servers,
    ingest_archive,
    parse_timestamp_from_name,
)
from chunkvault.store.progress import ProgressEvent

from tests._fixtures import ChunkSpec, write_region_file


# ---- helpers ---------------------------------------------------------------

def _make_server(root: Path, name: str, *, payload: bytes = b"x") -> Path:
    server = root / name
    server.mkdir(parents=True, exist_ok=True)
    world = server / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(b"placeholder")
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, payload),
    ])
    # Logs and crash-reports
    (server / "logs").mkdir()
    (server / "logs" / "latest.log").write_bytes(b"server log " + payload)
    (server / "crash-reports").mkdir()
    (server / "crash-reports" / "crash.txt").write_bytes(b"crash trace " + payload)
    # A config file (should not be captured into world or logs)
    (server / "server.properties").write_bytes(b"max-players=10")
    return server


def _zip_dir(zip_path: Path, root: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for entry in root.rglob("*"):
            if entry.is_file():
                arcname = entry.relative_to(root).as_posix()
                zf.write(entry, arcname)


# ---- timestamp parsing -----------------------------------------------------

def test_parse_timestamp_from_name():
    ts = parse_timestamp_from_name("2025-04-25-12-34-56.zip")
    assert ts == datetime(2025, 4, 25, 12, 34, 56, tzinfo=timezone.utc)


def test_parse_timestamp_embedded_in_name():
    ts = parse_timestamp_from_name("backup-2024-08-15-23-30-00-final.zip")
    assert ts == datetime(2024, 8, 15, 23, 30, 0, tzinfo=timezone.utc)


def test_parse_timestamp_missing():
    assert parse_timestamp_from_name("random-name.zip") is None
    assert parse_timestamp_from_name("backup.zip") is None


# ---- server discovery ------------------------------------------------------

def test_discover_two_servers(tmp_path: Path):
    src = tmp_path / "src"
    _make_server(src, "EX-Server")
    _make_server(src, "CR-Server")
    found = discover_servers(src)
    names = sorted(name for name, _ in found)
    assert names == ["CR-Server", "EX-Server"]


def test_discover_returns_world_root_not_server_root(tmp_path: Path):
    """The path returned is the WORLD dir (containing level.dat + region/),
    not the server root — so non-region file capture stays scoped."""
    src = tmp_path / "src"
    _make_server(src, "EX-Server")
    found = dict(discover_servers(src))
    assert found["EX-Server"].name == "world"
    assert (found["EX-Server"] / "level.dat").is_file()


def test_discover_falls_back_to_bare_world(tmp_path: Path):
    """Archive that's a bare world (no server wrapper)."""
    src = tmp_path / "src"
    src.mkdir()
    world = src / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(b"x")
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"x"),
    ])
    found = discover_servers(src)
    assert len(found) == 1


def test_discover_skips_dirs_with_no_region_data(tmp_path: Path):
    """An empty server directory (no region files) is not a real server."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "EX-Server").mkdir()
    (src / "EX-Server" / "config").mkdir()  # no world/, should be skipped
    _make_server(src, "CR-Server")           # has world + region + level.dat
    found = discover_servers(src)
    names = [name for name, _ in found]
    assert names == ["CR-Server"]


def test_discover_handles_custom_world_name(tmp_path: Path):
    """Multiverse / renamed worlds: world dir doesn't have to be 'world'."""
    src = tmp_path / "src"
    server = src / "EX-Server"
    server.mkdir(parents=True)
    survival = server / "survival"
    survival.mkdir()
    (survival / "level.dat").write_bytes(b"x")
    write_region_file(survival, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"x"),
    ])
    found = dict(discover_servers(src))
    assert "EX-Server" in found
    # world_root should be the `survival/` dir, not `EX-Server/`
    assert found["EX-Server"].name == "survival"


def test_discover_handles_bukkit_parallel_worlds(tmp_path: Path):
    """Bukkit puts world/, world_nether/, world_the_end/ directly under
    the server. world_root should be the server root in this case."""
    src = tmp_path / "src"
    server = src / "EX-Server"
    server.mkdir(parents=True)
    for sub in ("world", "world_nether", "world_the_end"):
        d = server / sub
        d.mkdir()
        if sub == "world":
            (d / "level.dat").write_bytes(b"x")
        write_region_file(d, "region", 0, 0, [
            ChunkSpec(0, 0, 1, 2, b"x"),
        ])
    found = dict(discover_servers(src))
    assert "EX-Server" in found
    assert found["EX-Server"].name == "EX-Server"  # falls back to server root


# ---- log collection --------------------------------------------------------

def test_collect_log_files_only_logs_and_crash_reports(tmp_path: Path):
    server = _make_server(tmp_path, "S")
    files = dict(collect_log_files(server))
    assert "logs/latest.log" in files
    assert "crash-reports/crash.txt" in files
    # Configs and world contents must NOT be picked up
    assert all(not p.startswith("world/") for p in files)
    assert "server.properties" not in files


def test_collect_log_files_handles_missing_dirs(tmp_path: Path):
    server = tmp_path / "no-logs-server"
    server.mkdir()
    (server / "world").mkdir()
    # No logs/ or crash-reports/ at all
    assert collect_log_files(server) == []


# ---- end-to-end ingest -----------------------------------------------------

def test_ingest_zip_creates_per_server_snapshots_and_log_snapshot(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    src = tmp_path / "src"
    _make_server(src, "EX-Server", payload=b"ex")
    _make_server(src, "CR-Server", payload=b"cr")
    archive = tmp_path / "2025-04-25-12-34-56.zip"
    _zip_dir(archive, src)

    result = ingest_archive(repo, archive)
    assert result.timestamp == datetime(2025, 4, 25, 12, 34, 56, tzinfo=timezone.utc)
    assert result.label == "2025-04-25-12-34-56"
    assert sorted(result.server_names) == ["CR-Server", "EX-Server"]
    assert len(result.snapshots) == 2
    assert result.log_snapshot is not None

    # World snapshots have correct labels and world_names
    snap_labels = {s.label for s in result.snapshots}
    assert snap_labels == {
        "EX-Server-2025-04-25-12-34-56",
        "CR-Server-2025-04-25-12-34-56",
    }
    snap_world_names = {s.world_name for s in result.snapshots}
    assert snap_world_names == {"EX-Server", "CR-Server"}

    # Log snapshot picked up logs from BOTH servers
    log_snap = repo.get_log_snapshot(result.log_snapshot.id)
    from chunkvault.store.log_manifest import read_log_manifest
    manifest = read_log_manifest(log_snap.manifest_path)
    assert set(manifest.servers) == {"EX-Server", "CR-Server"}
    for server, recs in manifest.servers.items():
        paths = sorted(r.relative_path for r in recs)
        assert paths == ["crash-reports/crash.txt", "logs/latest.log"]


def test_ingest_dedupes_logs_across_archives(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    src = tmp_path / "src"
    _make_server(src, "EX-Server", payload=b"same")
    a1 = tmp_path / "2025-04-01-00-00-00.zip"
    a2 = tmp_path / "2025-04-02-00-00-00.zip"
    _zip_dir(a1, src)
    _zip_dir(a2, src)

    ingest_archive(repo, a1)
    log_blobs_after_1 = sum(1 for p in (repo.repo_path / "logs").rglob("*")
                             if p.is_file())
    ingest_archive(repo, a2)
    log_blobs_after_2 = sum(1 for p in (repo.repo_path / "logs").rglob("*")
                             if p.is_file())
    assert log_blobs_after_2 == log_blobs_after_1  # logs identical → no new blob


def test_ingest_handles_no_servers(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    src = tmp_path / "src"
    src.mkdir()
    (src / "readme.txt").write_text("not a server")
    archive = tmp_path / "empty.zip"
    _zip_dir(archive, src)
    with pytest.raises(ImportError, match="no server folders"):
        ingest_archive(repo, archive)


def test_ingest_skip_logs_flag(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    src = tmp_path / "src"
    _make_server(src, "EX-Server")
    archive = tmp_path / "2025-01-01-00-00-00.zip"
    _zip_dir(archive, src)
    result = ingest_archive(repo, archive, skip_logs=True)
    assert result.log_snapshot is None
    # No log blobs were stored
    assert sum(1 for p in (repo.repo_path / "logs").rglob("*") if p.is_file()) == 0


def test_ingest_uses_filename_timestamp_as_world_label(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    src = tmp_path / "src"
    _make_server(src, "EX-Server")
    archive = tmp_path / "2025-04-25-12-34-56.zip"
    _zip_dir(archive, src)
    result = ingest_archive(repo, archive)
    snap = result.snapshots[0]
    # timestamp comes from filename, not from "now"
    assert snap.timestamp == datetime(2025, 4, 25, 12, 34, 56, tzinfo=timezone.utc)


def test_ingest_is_idempotent_on_re_run(tmp_path: Path):
    """Re-ingesting the same archive must not duplicate snapshots.

    Server snapshots are labelled `<server>-<ts>` — the second pass should
    see the existing label, skip the snapshot, and report it via
    ``already_ingested`` instead of creating a second row.
    """
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    src = tmp_path / "src"
    _make_server(src, "EX-Server")
    _make_server(src, "CR-Server")
    archive = tmp_path / "2025-04-25-12-34-56.zip"
    _zip_dir(archive, src)

    r1 = ingest_archive(repo, archive)
    assert len(r1.snapshots) == 2
    assert r1.already_ingested == []
    snap_count_after_first = len(repo.list())
    log_snap_count_after_first = len(repo.list_log_snapshots())

    r2 = ingest_archive(repo, archive)
    # Second pass: no NEW snapshots, but everything reported as already-ingested.
    assert r2.snapshots == []
    assert sorted(name for name, _ in r2.already_ingested) == [
        "CR-Server", "EX-Server",
    ]
    # Repo state unchanged.
    assert len(repo.list()) == snap_count_after_first
    assert len(repo.list_log_snapshots()) == log_snap_count_after_first


def test_ingest_handles_bukkit_layout(tmp_path: Path):
    """Bukkit's parallel-worlds layout (world/, world_nether/, world_the_end/
    all directly under the server root) gets snapshotted as one server with
    multiple dimensions."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    src = tmp_path / "src"
    server = src / "EX-Server"
    server.mkdir(parents=True)
    for sub in ("world", "world_nether", "world_the_end"):
        d = server / sub
        d.mkdir()
        if sub == "world":
            (d / "level.dat").write_bytes(b"placeholder")
        write_region_file(d, "region", 0, 0, [
            ChunkSpec(0, 0, 1, 2, sub.encode()),
        ])
    archive = tmp_path / "2025-04-25-12-34-56.zip"
    _zip_dir(archive, src)

    result = ingest_archive(repo, archive, verify_roundtrip=False)
    assert len(result.snapshots) == 1
    snap = result.snapshots[0]
    assert snap.world_name == "EX-Server"
    # All three dimensions captured in a single snapshot
    from chunkvault.store.manifest import read_manifest
    manifest = read_manifest(snap.manifest_path)
    dim_keys = sorted(manifest.dimensions.keys())
    assert dim_keys == [
        "world/region", "world_nether/region", "world_the_end/region",
    ]


def test_ingest_handles_custom_world_name(tmp_path: Path):
    """A server whose world dir is named 'survival' (not 'world') still
    gets discovered — the name isn't hardcoded."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    src = tmp_path / "src"
    server = src / "EX-Server"
    server.mkdir(parents=True)
    survival = server / "survival"
    survival.mkdir()
    (survival / "level.dat").write_bytes(b"placeholder")
    write_region_file(survival, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"x"),
    ])
    (server / "logs").mkdir()
    (server / "logs" / "latest.log").write_bytes(b"log\n")
    archive = tmp_path / "2025-04-25-12-34-56.zip"
    _zip_dir(archive, src)

    result = ingest_archive(repo, archive, verify_roundtrip=False)
    assert len(result.snapshots) == 1
    snap = result.snapshots[0]
    assert snap.world_name == "EX-Server"
    # Logs still found (sit at server root, not inside the world dir)
    assert result.log_snapshot is not None


def test_ingest_progress_callback_fires(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    src = tmp_path / "src"
    _make_server(src, "EX-Server")
    _make_server(src, "CR-Server")
    archive = tmp_path / "2025-04-25-12-34-56.zip"
    _zip_dir(archive, src)
    events: list[ProgressEvent] = []
    ingest_archive(repo, archive, progress_cb=events.append)
    phases = {e.phase for e in events}
    assert "ingest_archive" in phases
    assert "server_world" in phases
    assert "logs" in phases
    assert events[-1].kind == "finish"
