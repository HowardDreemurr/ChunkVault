from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

import pytest

from chunkvault.store.importer import ImportError_ as ImportError, ImportSession, iter_archives

from tests._fixtures import ChunkSpec, write_region_file


def _make_world(root: Path, name: str = "world") -> Path:
    world = root / name
    world.mkdir(parents=True, exist_ok=True)
    (world / "level.dat").write_bytes(b"placeholder level.dat")
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"chunk-content"),
    ])
    return world


def _make_zip(zip_path: Path, base_dir: Path, world_dir_name: str | None = None) -> None:
    """Zip the contents of base_dir. If world_dir_name is given, wrap inside it."""
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for entry in base_dir.rglob("*"):
            if entry.is_file():
                arcname = entry.relative_to(base_dir).as_posix()
                if world_dir_name:
                    arcname = f"{world_dir_name}/{arcname}"
                zf.write(entry, arcname)


def _make_targz(out_path: Path, base_dir: Path, world_dir_name: str | None = None) -> None:
    with tarfile.open(out_path, "w:gz") as tf:
        for entry in base_dir.rglob("*"):
            if entry.is_file():
                arcname = entry.relative_to(base_dir).as_posix()
                if world_dir_name:
                    arcname = f"{world_dir_name}/{arcname}"
                tf.add(entry, arcname=arcname)


# ---- pass-through directory ------------------------------------------------

def test_import_plain_directory_passthrough(tmp_path: Path):
    world = _make_world(tmp_path)
    with ImportSession(world) as resolved:
        assert resolved == world
        assert (resolved / "level.dat").is_file()


def test_import_directory_with_nested_world(tmp_path: Path):
    """Common shape: outer dir contains a 'world' subdir with the actual save."""
    outer = tmp_path / "server-backup-2024"
    outer.mkdir()
    _make_world(outer, "world")
    with ImportSession(outer) as resolved:
        assert resolved.name == "world"
        assert (resolved / "level.dat").is_file()


def test_import_missing_source_raises(tmp_path: Path):
    with pytest.raises(ImportError, match="does not exist"):
        with ImportSession(tmp_path / "nope") as _:
            pass


def test_import_directory_without_world_raises(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "readme.txt").write_text("not a world")
    with pytest.raises(ImportError, match="could not find a world"):
        with ImportSession(empty) as _:
            pass


# ---- zip ------------------------------------------------------------------

def test_import_zip_with_world_at_root(tmp_path: Path):
    world = _make_world(tmp_path / "src", "world")
    zip_path = tmp_path / "backup.zip"
    _make_zip(zip_path, world)  # contents directly at root

    with ImportSession(zip_path) as resolved:
        # The extracted root itself is the world (level.dat sits there)
        assert (resolved / "level.dat").is_file()
        assert (resolved / "region" / "r.0.0.mca").is_file()


def test_import_zip_with_world_nested(tmp_path: Path):
    world = _make_world(tmp_path / "src", "world")
    zip_path = tmp_path / "backup.zip"
    _make_zip(zip_path, world, world_dir_name="my-world")

    with ImportSession(zip_path) as resolved:
        assert resolved.name == "my-world"
        assert (resolved / "level.dat").is_file()


# ---- tar.gz ---------------------------------------------------------------

def test_import_targz_passthrough(tmp_path: Path):
    world = _make_world(tmp_path / "src", "world")
    tar_path = tmp_path / "backup.tar.gz"
    _make_targz(tar_path, world)

    with ImportSession(tar_path) as resolved:
        assert (resolved / "level.dat").is_file()


def test_import_tgz_extension_recognized(tmp_path: Path):
    world = _make_world(tmp_path / "src", "world")
    tar_path = tmp_path / "backup.tgz"
    _make_targz(tar_path, world, world_dir_name="myworld")

    with ImportSession(tar_path) as resolved:
        assert resolved.name == "myworld"


# ---- world_subpath override ------------------------------------------------

def test_import_zip_with_explicit_world_subpath(tmp_path: Path):
    """Some archives have multiple worlds; user can pick one explicitly."""
    src = tmp_path / "src"
    src.mkdir()
    _make_world(src, "world-a")
    _make_world(src, "world-b")
    zip_path = tmp_path / "backup.zip"
    _make_zip(zip_path, src)

    with ImportSession(zip_path, world_subpath="world-b") as resolved:
        assert resolved.name == "world-b"


def test_import_invalid_world_subpath_raises(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    _make_world(src, "world")
    zip_path = tmp_path / "backup.zip"
    _make_zip(zip_path, src)

    with pytest.raises(ImportError, match="world-subpath"):
        with ImportSession(zip_path, world_subpath="does-not-exist") as _:
            pass


# ---- cleanup --------------------------------------------------------------

def test_temp_extraction_cleaned_up_on_exit(tmp_path: Path):
    world = _make_world(tmp_path / "src", "world")
    zip_path = tmp_path / "backup.zip"
    _make_zip(zip_path, world)

    session = ImportSession(zip_path)
    extracted: Path
    with session as resolved:
        extracted = resolved
        assert extracted.exists()
    # After context exit, the temp dir should be gone
    assert not extracted.exists()


# ---- default label --------------------------------------------------------

def test_default_label_strips_archive_suffix(tmp_path: Path):
    assert ImportSession(tmp_path / "smp-2024-01-01.zip").default_label \
        == "smp-2024-01-01"
    assert ImportSession(tmp_path / "world.tar.gz").default_label == "world"
    assert ImportSession(tmp_path / "world.tgz").default_label == "world"
    assert ImportSession(tmp_path / "world.tar.bz2").default_label == "world"
    assert ImportSession(tmp_path / "world.tar").default_label == "world"
    assert ImportSession(tmp_path / "raw-dir").default_label == "raw-dir"


# ---- bulk archive iteration ------------------------------------------------

def test_iter_archives_finds_supported_files(tmp_path: Path):
    (tmp_path / "a.zip").write_bytes(b"")
    (tmp_path / "b.tar.gz").write_bytes(b"")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.tgz").write_bytes(b"")
    (tmp_path / "sub" / "readme.txt").write_text("ignore")
    (tmp_path / "manifest.json").write_text("ignore")

    found = sorted(p.name for p in iter_archives(tmp_path))
    assert found == ["a.zip", "b.tar.gz", "c.tgz"]


# ---- end-to-end import → snapshot ------------------------------------------

def test_import_then_snapshot_roundtrip(tmp_path: Path):
    """Realistic flow: zip a world, import-and-snapshot, restore, compare."""
    from chunkvault.store import ChunkSnapshotRepo

    world = _make_world(tmp_path / "src", "world")
    zip_path = tmp_path / "backup.zip"
    _make_zip(zip_path, world, world_dir_name="my-world")

    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    with ImportSession(zip_path) as imported:
        snap = repo.snapshot(imported, label="from-zip")

    assert snap.label == "from-zip"
    dest = tmp_path / "restored"
    repo.restore(snap, dest)
    assert (dest / "level.dat").read_bytes() == b"placeholder level.dat"
    assert (dest / "region" / "r.0.0.mca").is_file()
