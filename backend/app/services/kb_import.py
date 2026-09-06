"""Import of the Savvi knowledge-base export into kb_documents.

Owner decision (2026-08-31): everything from the export goes in AS IS,
as drafts. Gaps and contradictions in the data do NOT block the import —
the owner edits them in the admin afterwards.

Idempotent: documents are matched by (title, category); a document the
owner has already edited by hand (version > 1) is never overwritten.
Drafts are not indexed, so importing costs nothing in embeddings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from app.models import KbDocument, KbProcessingLog
from app.models.enums import DocStatus, KbLanguage, SourceType

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class BlockMapping:
    title: str
    category: str
    #: Savvi kept this block switched off; keep it out of retrieval.
    searchable: bool = True


# Mapping fixed in docs/knowledge-base-import.md §2.
MAPPING: dict[str, BlockMapping] = {
    "абонементы в тренажерный зал": BlockMapping("Тренажёрный зал: абонементы", "gym"),
    "клубные карты в тренажерный зал": BlockMapping("Тренажёрный зал: клубные карты", "gym"),
    "заморозка абонементов в тренажерный зал": BlockMapping(
        "Тренажёрный зал: заморозка абонементов", "gym"
    ),
    "информация об организации": BlockMapping("Об организации", "org"),
    "аренда залов/футбольного поля": BlockMapping("Аренда залов и полей", "rental"),
    "спортивные секции": BlockMapping("Спортивные секции", "sections"),
    "тир, страйкбол": BlockMapping("Тир и страйкбол", "tir"),
    "гостиница": BlockMapping("Гостиница и столовая", "hotel"),
    # Savvi had this block disabled ("НЕ ВКЛЮЧЕНО") and it is seasonal.
    "акции (не включено)": BlockMapping("Акции", "promo", searchable=False),
}

# Blocks that are configuration or prompt material, not knowledge.
SKIP_BLOCKS = {"база знаний", "менеджер", "доп настройки", "параметры ответов", "безопасность"}

_BOLD_LINE_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")
_ESCAPES = {"\\#": "#", "\\-": "-", "\\=": "=", "\\_": "_", "\\*": "*", "\\.": "."}
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?7|8)[\s\-]?\(?(\d{3})\)?[\s\-]?(\d{3})[\s\-]?(\d{2})[\s\-]?(\d{2})(?!\d)"
)


@dataclass
class ImportReport:
    created: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"Создано документов: {len(self.created)}"]
        lines += [f"  + {title}" for title in self.created]
        if self.skipped_existing:
            lines.append(
                f"Пропущено (уже есть, правки владельца сохранены): {len(self.skipped_existing)}"
            )
            lines += [f"  = {title}" for title in self.skipped_existing]
        if self.unmapped:
            lines.append(f"Блоки без сопоставления (не импортированы): {len(self.unmapped)}")
            lines += [f"  ? {title}" for title in self.unmapped]
        return "\n".join(lines)


def normalize_body(text: str) -> str:
    """Undo Savvi's markdown escaping, fix an unambiguous typo, and put
    phone numbers into one display format. Nothing else is edited — the
    owner reviews the content in the admin."""
    for escaped, plain in _ESCAPES.items():
        text = text.replace(escaped, plain)
    text = text.replace("16:00–19:0\n", "16:00–19:00\n")  # obvious typo in the export
    text = _PHONE_RE.sub(lambda m: f"+7 {m.group(1)} {m.group(2)} {m.group(3)} {m.group(4)}", text)
    # Trailing double spaces are markdown line breaks in the export.
    lines = [line.rstrip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# A heading counts as an English screenshot caption (not a KB block name)
# when this share of its letters is Latin.
_LATIN_CAPTION_RATIO = 0.8


def _is_latin_caption(heading: str) -> bool:
    letters = [ch for ch in heading if ch.isalpha()]
    if not letters:
        return False
    latin = sum(1 for ch in letters if ch.isascii())
    return latin / len(letters) > _LATIN_CAPTION_RATIO


def parse_blocks(markdown: str) -> list[tuple[str, str]]:
    """Split the export into (heading, body) pairs.

    Headings are bold-only lines. A bold line immediately following a
    heading is the English caption and is treated as part of the heading,
    not as a new block.
    """
    blocks: list[tuple[str, list[str]]] = []
    previous_was_heading = False
    for raw_line in markdown.splitlines():
        match = _BOLD_LINE_RE.match(raw_line.strip())
        if match:
            heading = match.group(1).replace("\\", "").strip()
            if previous_was_heading and _is_latin_caption(heading):
                # "Gym Memberships" under "Абонементы..." — a caption,
                # not a new block. A Cyrillic line IS a new block, even
                # right after a section heading like "База знаний".
                previous_was_heading = False
                continue
            blocks.append((heading, []))
            previous_was_heading = True
            continue
        previous_was_heading = False
        if blocks:
            blocks[-1][1].append(raw_line)
    return [(title, "\n".join(body)) for title, body in blocks]


async def import_savvi_export(
    db: AsyncSession, source: Path, author_user_id: int | None = None
) -> ImportReport:
    markdown = source.read_text(encoding="utf-8")
    report = ImportReport()

    for heading, body in parse_blocks(markdown):
        key = heading.casefold().strip()
        if key in SKIP_BLOCKS or not body.strip():
            continue
        mapping = MAPPING.get(key)
        if mapping is None:
            report.unmapped.append(heading)
            continue

        existing = (
            await db.execute(
                select(KbDocument).where(
                    KbDocument.title == mapping.title,
                    KbDocument.category == mapping.category,
                    KbDocument.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            report.skipped_existing.append(mapping.title)
            continue

        document = KbDocument(
            title=mapping.title,
            category=mapping.category,
            language=KbLanguage.RU,
            source_type=SourceType.MANUAL_TEXT,
            # Imported content is never auto-published: the owner reviews
            # and activates it (drafts are not indexed, so this is free).
            status=DocStatus.DRAFT,
            is_searchable=mapping.searchable,
            body=normalize_body(body),
            author_user_id=author_user_id,
            index_state="not_indexed",
        )
        db.add(document)
        await db.flush()
        db.add(
            KbProcessingLog(
                document_id=document.id,
                step="import",
                status="ok",
                detail=f"импортировано из экспорта Савви, блок «{heading}»",
            )
        )
        report.created.append(mapping.title)

    logger.info(
        "kb_import_done",
        created=len(report.created),
        skipped=len(report.skipped_existing),
        unmapped=len(report.unmapped),
    )
    return report
