"""Long-running ``git cat-file --batch`` subprocess for cheap blob lookup.

Spawning ``git`` for every cat-file call costs ~10 ms per invocation on
Windows. For a diff that fetches 2 × 500 region blobs that's up to 10 seconds
of pure fork overhead. ``cat-file --batch`` takes one revspec per line on
stdin and streams the resulting blobs out stdout, so the whole batch pays the
fork cost once.

Output format per request::

    <sha> <type> <size>\\n
    <size bytes of payload>
    \\n

For a missing or unresolvable revspec::

    <revspec> missing\\n

This class wraps that protocol as a simple ``fetch(revspec) -> bytes | None``
and cleans up the subprocess on ``close()`` / context exit.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


class BatchCatFile:
    """Context-manager wrapper around ``git cat-file --batch``."""

    def __init__(self, git_dir: Path | str):
        self._git_dir = str(git_dir)
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> "BatchCatFile":
        env = os.environ.copy()
        env["GIT_DIR"] = self._git_dir
        self._proc = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=0,
        )
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        self._proc = None

    def fetch(self, revspec: str) -> bytes | None:
        """Return the blob bytes for ``revspec`` (e.g. ``"<sha>:path/to.mca"``).

        Returns ``None`` if the revspec doesn't resolve to a blob.
        """
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("BatchCatFile not entered (use as a context manager)")
        self._proc.stdin.write(revspec.encode("utf-8") + b"\n")
        self._proc.stdin.flush()

        header = self._read_line(self._proc.stdout)
        if header.endswith(b" missing"):
            return None
        # Header is "<sha> <type> <size>"
        try:
            parts = header.split()
            size = int(parts[-1])
        except (ValueError, IndexError) as e:
            raise RuntimeError(
                f"unparseable cat-file header: {header!r}"
            ) from e
        body = _read_exact(self._proc.stdout, size)
        trailing = self._proc.stdout.read(1)
        if trailing != b"\n":
            # Shouldn't happen for well-formed git output; treat as a hard fail.
            raise RuntimeError(
                f"missing trailing newline after blob for {revspec!r}"
            )
        return body

    @staticmethod
    def _read_line(stream) -> bytes:
        buf = bytearray()
        while True:
            ch = stream.read(1)
            if not ch:
                raise RuntimeError("unexpected EOF reading cat-file header")
            if ch == b"\n":
                return bytes(buf)
            buf += ch


def _read_exact(stream, n: int) -> bytes:
    """Read exactly n bytes or raise. subprocess pipes sometimes return short reads."""
    out = bytearray()
    while len(out) < n:
        chunk = stream.read(n - len(out))
        if not chunk:
            raise RuntimeError(f"short read: wanted {n} bytes, got {len(out)}")
        out += chunk
    return bytes(out)
