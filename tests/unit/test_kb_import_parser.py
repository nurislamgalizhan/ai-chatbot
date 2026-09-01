"""Parsing of the real Savvi export file."""

from __future__ import annotations

from pathlib import Path

from app.services.kb_import import MAPPING, normalize_body, parse_blocks

EXPORT = Path(__file__).resolve().parents[2] / "seed" / "savvi-export.md"


def test_real_export_parses_into_known_blocks() -> None:
    blocks = dict(parse_blocks(EXPORT.read_text(encoding="utf-8")))
    keys = {title.casefold().strip() for title in blocks}
    # Every mapped block must actually be found in the export.
    for mapped_key in MAPPING:
        assert mapped_key in keys, f"block missing from export: {mapped_key}"


def test_english_caption_is_not_a_separate_block() -> None:
    markdown = "**Гостиница**\n**Hotel**\nКойко-место 8000 тенге/сутки\n"
    blocks = parse_blocks(markdown)
    assert [title for title, _ in blocks] == ["Гостиница"]
    assert "Койко-место" in blocks[0][1]


def test_normalize_body_unescapes_and_formats_phones() -> None:
    raw = "Александр \\- 87774005676\nСтоимость \\= 9000 тенге"
    normalized = normalize_body(raw)
    assert "\\-" not in normalized
    assert "\\=" not in normalized
    assert "+7 777 400 56 76" in normalized


def test_normalize_body_fixes_known_typo() -> None:
    assert "16:00–19:00" in normalize_body("8:00–11:00 16:00–19:0\nследующая строка")
