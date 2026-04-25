"""Wizard flow tests — drive the prompts via monkey-patched input/Confirm."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from rich.console import Console

import chunkvault.wizard.flows as flows
import chunkvault.wizard.ui as ui
from chunkvault.store import ChunkSnapshotRepo

from tests._fixtures import ChunkSpec, write_region_file


def _make_console() -> Console:
    """A Console that captures output to a string buffer."""
    return Console(file=io.StringIO(), force_terminal=False, width=120)


class FakePrompt:
    """Replace rich.prompt.Prompt.ask with a deterministic queue of answers."""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.calls: list[str] = []

    def __call__(self, question, *args, default=None, choices=None, **kwargs):
        self.calls.append(str(question))
        if not self.answers:
            return default if default is not None else ""
        return self.answers.pop(0)


class FakeConfirm:
    def __init__(self, answers: list[bool]):
        self.answers = list(answers)
        self.calls: list[str] = []

    def __call__(self, question, *args, default=True, **kwargs):
        self.calls.append(str(question))
        if not self.answers:
            return default
        return self.answers.pop(0)


def _make_world(root: Path, name: str = "world") -> Path:
    world = root / name
    world.mkdir()
    (world / "level.dat").write_bytes(b"placeholder")
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"x"),
    ])
    return world


def _make_server_zip(tmp_path: Path, server_names: list[str]) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    for name in server_names:
        server = src / name
        server.mkdir()
        world = server / "world"
        world.mkdir()
        (world / "level.dat").write_bytes(b"placeholder")
        write_region_file(world, "region", 0, 0, [
            ChunkSpec(0, 0, 1, 2, name.encode()),
        ])
        # logs so the ingest produces a log_snapshot
        (server / "logs").mkdir()
        (server / "logs" / "latest.log").write_bytes(f"log of {name}".encode())
    archive = tmp_path / "2025-04-25-12-34-56.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for entry in src.rglob("*"):
            if entry.is_file():
                zf.write(entry, entry.relative_to(src).as_posix())
    return archive


# ---- ingest flow -----------------------------------------------------------

def test_ingest_flow_runs_through_archives(tmp_path: Path, monkeypatch):
    """Drive the ingest flow with canned answers; assert it ingests."""
    from chunkvault.wizard import detect

    repo_path = tmp_path / "repo"
    archive = _make_server_zip(tmp_path, ["EX-Server", "CR-Server"])

    # Fake env: tell the flow about our archive directory
    env = detect.detect_environment(
        repo_candidates=[],
        source_candidates=[archive.parent],
    )

    # Answers in the order asked:
    #   1. repo path           → tmp_path / "repo"
    #   2. (init? confirm)     → True
    #   3. (use source path?)  → True
    #   4. (add another?)      → False
    #   5. (capture logs?)     → True
    #   6. (verify each?)      → False (skip — tests don't need the slow path)
    # (Per-archive selection replaces the old "proceed?" prompt — we mock
    #  select_archives to accept everything below.)
    fake_prompt = FakePrompt([str(repo_path)])
    fake_confirm = FakeConfirm([True, True, False, True, False])
    monkeypatch.setattr("chunkvault.wizard.flows.prompt_path",
                        lambda c, lbl, **kw: Path(fake_prompt(lbl, default=kw.get("default"))))
    monkeypatch.setattr("chunkvault.wizard.flows.confirm",
                        lambda c, lbl, default=True: fake_confirm(lbl, default=default))
    # questionary.checkbox needs a real Windows console — bypass it in tests.
    monkeypatch.setattr("chunkvault.wizard.flows.select_archives",
                        lambda archives, previews: list(archives))

    console = _make_console()
    results = flows.run_ingest_flow(console, env)
    assert len(results) == 1
    assert len(results[0].snapshots) == 2
    assert results[0].log_snapshot is not None

    # The repo should now exist
    repo = ChunkSnapshotRepo(repo_path)
    assert repo.is_initialized()
    assert len(repo.list()) == 2


def test_ingest_flow_aborts_when_user_declines(tmp_path: Path, monkeypatch):
    from chunkvault.wizard import detect

    archive = _make_server_zip(tmp_path, ["EX-Server"])
    env = detect.detect_environment(
        repo_candidates=[],
        source_candidates=[archive.parent],
    )
    repo_path = tmp_path / "repo"

    fake_prompt = FakePrompt([str(repo_path)])
    # init=yes, use source=yes, add another=no, capture logs=yes, verify=no
    fake_confirm = FakeConfirm([True, True, False, True, False])
    monkeypatch.setattr("chunkvault.wizard.flows.prompt_path",
                        lambda c, lbl, **kw: Path(fake_prompt(lbl, default=kw.get("default"))))
    monkeypatch.setattr("chunkvault.wizard.flows.confirm",
                        lambda c, lbl, default=True: fake_confirm(lbl, default=default))
    # User aborts the per-archive selection (returns empty list)
    monkeypatch.setattr("chunkvault.wizard.flows.select_archives",
                        lambda archives, previews: [])

    console = _make_console()
    results = flows.run_ingest_flow(console, env)
    assert results == []  # cancelled


def test_ingest_flow_handles_no_sources(tmp_path: Path, monkeypatch):
    """If user picks no sources, the flow exits gracefully."""
    from chunkvault.wizard import detect

    env = detect.EnvironmentSummary(repos=[], source_paths=[])
    repo_path = tmp_path / "repo"

    fake_prompt = FakePrompt([str(repo_path)])
    fake_confirm = FakeConfirm([True, False])  # init=yes, add-another=no
    monkeypatch.setattr("chunkvault.wizard.flows.prompt_path",
                        lambda c, lbl, **kw: Path(fake_prompt(lbl, default=kw.get("default"))))
    monkeypatch.setattr("chunkvault.wizard.flows.confirm",
                        lambda c, lbl, default=True: fake_confirm(lbl, default=default))

    console = _make_console()
    results = flows.run_ingest_flow(console, env)
    assert results == []
