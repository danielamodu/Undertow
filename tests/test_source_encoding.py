"""Guards against text that is valid UTF-8 but wrong.

`statistical.py` shipped with three-character sequences where it meant `→` and
two where it meant `σ` — UTF-8 bytes that had been read as cp1252 and saved
again. The file decoded without error, every test passed, and the damage only
showed up in the one place nobody was looking: the summary line of a statistical
finding.

(The broken forms are not written out anywhere in this file. They are generated
below, so that this module does not trip the very check it defines.)

It stayed hidden because the statistical differ never ran against a live DataHub
— profiles are a timeseries aspect the resolver was not fetching — so no human
ever read one of those strings. Two defects, each concealing the other.

This scans for the byte sequences that only occur after that round trip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SEARCH_ROOTS = ("src", "tests", "scripts", "contrib", "examples", "docs", "skills")
EXTENSIONS = {".py", ".md", ".yaml", ".yml", ".txt", ".toml", ".json", ".sql"}

# The characters this codebase actually uses that would be mangled by the round
# trip. The broken forms are derived rather than written out: spelling them as
# literals would put them in this file, and the scan below would then flag its
# own source. Deriving them also states the mechanism exactly — encode as UTF-8,
# decode as cp1252 — instead of asking a reader to trust a table of noise.
AT_RISK = "—–→σ’“”"


def mangled(char: str) -> str | None:
    try:
        return char.encode("utf-8").decode("cp1252")
    except UnicodeDecodeError:
        # Not every byte sequence has a cp1252 mapping; those characters cannot
        # be produced by this particular round trip, so they need no guard.
        return None


MOJIBAKE = {
    broken: char for char in AT_RISK if (broken := mangled(char)) is not None
}


def source_files() -> list[Path]:
    files = [
        path
        for root in SEARCH_ROOTS
        for path in (ROOT / root).rglob("*")
        if path.is_file()
        and path.suffix in EXTENSIONS
        and "__pycache__" not in path.parts
    ]
    files += [
        ROOT / name
        for name in ("README.md", "undertow.yaml", "pyproject.toml")
        if (ROOT / name).exists()
    ]
    return files


@pytest.mark.parametrize("path", source_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_double_encoded_text(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    found = {seq: char for seq, char in MOJIBAKE.items() if seq in text}

    assert not found, (
        f"{path.relative_to(ROOT)} contains double-encoded text: "
        + ", ".join(f"{seq!r} should be {char!r}" for seq, char in found.items())
    )


def test_every_file_decodes_as_utf8() -> None:
    """A file that is not UTF-8 at all breaks on someone else's locale."""
    undecodable = []
    for path in source_files():
        try:
            path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            undecodable.append(f"{path.relative_to(ROOT)}: {exc}")

    assert not undecodable, "not valid UTF-8:\n" + "\n".join(undecodable)


def test_the_statistical_summaries_use_real_symbols() -> None:
    """The specific strings that were broken, pinned by content.

    A user reads these. They are the only place `σ` and `→` reach a terminal.
    """
    source = (ROOT / "src" / "undertow" / "differ" / "statistical.py").read_text(
        encoding="utf-8"
    )

    assert "σ" in source
    assert "→" in source
    assert "â" not in source
