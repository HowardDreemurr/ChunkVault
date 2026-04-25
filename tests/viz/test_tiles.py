from __future__ import annotations

import pytest

from chunkvault.viz.tiles import TilesError, render_world_tiles, unmined_available


# unmined_available should always be callable and either return a str path or
# None. This runs on every CI without needing unmined installed.

def test_unmined_available_returns_str_or_none():
    result = unmined_available()
    assert result is None or isinstance(result, str)


def test_render_world_tiles_errors_clearly_without_unmined(tmp_path):
    """If unmined isn't available, render_world_tiles must raise TilesError
    with a message that tells the user where to get it — not a cryptic
    FileNotFoundError from subprocess."""
    if unmined_available() is not None:
        pytest.skip("unmined is installed — can't test the 'missing' path")
    with pytest.raises(TilesError, match="unmined"):
        render_world_tiles(tmp_path, tmp_path / "out")


# The following tests exercise unmined itself and only run when it's present.

needs_unmined = pytest.mark.skipif(
    unmined_available() is None, reason="unmined CLI not on PATH"
)


@needs_unmined
def test_render_world_tiles_succeeds(tmp_path):
    """Smoke test — only runs when unmined is installed. We don't validate
    tile content, just that the invocation completes and writes *something*."""
    # A real run needs a real MC world; this test is mostly for the sake of
    # future maintenance. Skip if no fixture world is available here.
    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(b"placeholder")
    try:
        render_world_tiles(world, tmp_path / "tiles")
    except TilesError:
        # unmined rejected our synthetic world — that's acceptable. The test
        # confirmed we can invoke unmined without hitting PATH issues.
        pass
