"""Knowledge-base API: documents lifecycle, FAQ management with the
language policy, translation review, misses analytics, import."""

from __future__ import annotations

from pathlib import Path

from app.models import KbDocument, KbFaq
from app.services.kb_import import import_savvi_export
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

EXPORT = Path(__file__).resolve().parents[2] / "seed" / "savvi-export.md"

FAQ_PAYLOAD = {
    "normalized_key": "address",
    "category": "org",
    "answer_ru": "Адрес: ул. Коперника 130.",
    "patterns_ru": ["какой у вас адрес", "где вы находитесь"],
    "patterns_kk": ["мекенжайыңыз қандай"],
}


async def test_document_lifecycle_marks_indexing(authed_client, db_engine) -> None:
    client, headers = authed_client
    created = await client.post(
        "/api/kb/documents",
        json={
            "title": "Аренда залов",
            "body": "Зал 26000 тенге/час",
            "category": "rental",
        },
        headers=headers,
    )
    assert created.status_code == 200
    doc = created.json()
    # Drafts are not queued for embedding — importing costs nothing.
    assert doc["status"] == "draft"
    assert doc["index_state"] == "not_indexed"

    activated = await client.patch(
        f"/api/kb/documents/{doc['id']}", json={"status": "active"}, headers=headers
    )
    assert activated.json()["index_state"] == "pending"

    edited = await client.patch(
        f"/api/kb/documents/{doc['id']}",
        json={"body": "Зал 27000 тенге/час"},
        headers=headers,
    )
    assert edited.json()["version"] == 2
    assert edited.json()["index_state"] == "pending"

    switched_off = await client.patch(
        f"/api/kb/documents/{doc['id']}", json={"is_searchable": False}, headers=headers
    )
    assert switched_off.json()["index_state"] == "not_indexed"


async def test_duplicate_document_title_conflicts(authed_client) -> None:
    client, headers = authed_client
    payload = {"title": "Об организации", "body": "текст", "category": "org"}
    assert (
        await client.post("/api/kb/documents", json=payload, headers=headers)
    ).status_code == 200
    duplicate = await client.post("/api/kb/documents", json=payload, headers=headers)
    assert duplicate.status_code == 409


async def test_soft_delete_removes_chunks_and_hides_document(
    authed_client, db_engine
) -> None:
    client, headers = authed_client
    doc = (
        await client.post(
            "/api/kb/documents",
            json={
                "title": "Тир",
                "body": "Стрельба",
                "category": "tir",
                "status": "active",
            },
            headers=headers,
        )
    ).json()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        await db.execute(
            text(
                "INSERT INTO kb_chunks (document_id, chunk_index, content, token_count,"
                " embedding, metadata, embedding_model) VALUES (:d, 0, 'x', 1,"
                " :v, '{}'::jsonb, 'test')"
            ),
            {"d": doc["id"], "v": "[" + ",".join(["0.1"] * 1536) + "]"},
        )

    assert (
        await client.delete(f"/api/kb/documents/{doc['id']}", headers=headers)
    ).status_code == 204
    assert (await client.get(f"/api/kb/documents/{doc['id']}")).status_code == 404

    async with maker() as db:
        chunks = (await db.execute(text("SELECT count(*) FROM kb_chunks"))).scalar_one()
        document = await db.get(KbDocument, doc["id"])
    assert chunks == 0  # nothing deleted can ever surface in retrieval
    assert document.deleted_at is not None  # restorable row kept


async def test_faq_creation_activation_and_language_gates(authed_client) -> None:
    client, headers = authed_client
    faq = (await client.post("/api/kb/faq", json=FAQ_PAYLOAD, headers=headers)).json()
    assert faq["answer_ru_status"] == "approved"
    assert faq["answer_kk_status"] == "absent"
    assert faq["is_active"] is False
    assert {p["language"] for p in faq["patterns"]} == {"ru", "kk"}

    activated = await client.post(
        f"/api/kb/faq/{faq['id']}/activate", json={"is_active": True}, headers=headers
    )
    assert activated.status_code == 200
    assert activated.json()["is_active"] is True


async def test_faq_activation_requires_kk_patterns(authed_client) -> None:
    """kk patterns are mandatory even when there is no kk answer yet."""
    client, headers = authed_client
    payload = {**FAQ_PAYLOAD, "normalized_key": "phones", "patterns_kk": []}
    response = await client.post("/api/kb/faq", json=payload, headers=headers)
    assert response.status_code == 422  # schema requires at least one kk pattern


async def test_machine_translation_flow_and_single_entry_approval(
    authed_client,
) -> None:
    client, headers = authed_client
    faq = (await client.post("/api/kb/faq", json=FAQ_PAYLOAD, headers=headers)).json()

    drafted = await client.patch(
        f"/api/kb/faq/{faq['id']}/answer",
        json={"language": "kk", "answer": "Машинный черновик", "status": "machine"},
        headers=headers,
    )
    assert drafted.json()["answer_kk_status"] == "machine"

    review = (
        await client.get("/api/kb/faq/translations?language=kk&status=machine")
    ).json()
    assert len(review["items"]) == 1
    assert review["items"][0]["source_ru"] == FAQ_PAYLOAD["answer_ru"]
    assert review["items"][0]["draft"] == "Машинный черновик"

    approved = await client.patch(
        f"/api/kb/faq/{faq['id']}/answer",
        json={
            "language": "kk",
            "answer": "Мекенжайы: Коперник 130",
            "status": "approved",
        },
        headers=headers,
    )
    assert approved.json()["answer_kk_status"] == "approved"

    # There is no bulk-approve endpoint at all — approval is per entry.
    missing = await client.post("/api/kb/faq/approve-all", json={}, headers=headers)
    assert missing.status_code in (404, 405)


async def test_empty_answer_cannot_be_approved(authed_client) -> None:
    client, headers = authed_client
    faq = (await client.post("/api/kb/faq", json=FAQ_PAYLOAD, headers=headers)).json()
    response = await client.patch(
        f"/api/kb/faq/{faq['id']}/answer",
        json={"language": "kk", "answer": "   ", "status": "approved"},
        headers=headers,
    )
    assert response.status_code == 422


async def test_language_miss_analytics(authed_client, db_engine) -> None:
    client, headers = authed_client
    faq = (await client.post("/api/kb/faq", json=FAQ_PAYLOAD, headers=headers)).json()
    await client.post(
        f"/api/kb/faq/{faq['id']}/activate", json={"is_active": True}, headers=headers
    )

    # A Kazakh question with no approved kk answer -> miss.
    preview = await client.post(
        "/api/kb/search", json={"query": "мекенжайыңыз қандай"}, headers=headers
    )
    assert preview.status_code == 200
    assert preview.json()["detected_language"] == "kk"
    assert preview.json()["faq_hit"] is None
    assert preview.json()["faq_language_miss"] is True

    misses = (await client.get("/api/kb/faq/language-misses")).json()
    assert misses["total"] >= 1
    assert misses["items"][0]["language"] == "kk"
    assert misses["items"][0]["normalized_key"] == "address"


async def test_search_preview_returns_faq_answer_without_llm(authed_client) -> None:
    client, headers = authed_client
    faq = (await client.post("/api/kb/faq", json=FAQ_PAYLOAD, headers=headers)).json()
    await client.post(
        f"/api/kb/faq/{faq['id']}/activate", json={"is_active": True}, headers=headers
    )
    response = await client.post(
        "/api/kb/search", json={"query": "какой у вас адрес"}, headers=headers
    )
    body = response.json()
    assert body["faq_hit"] == FAQ_PAYLOAD["answer_ru"]
    assert body["faq_language_miss"] is False
    assert body["hits"] == []  # no embedding provider configured in tests


async def test_search_preview_is_rate_limited(authed_client) -> None:
    """Недочёт №3: the preview embeds the query (a paid call), so a
    runaway tab or script must hit a 429, not the OpenAI bill."""
    client, headers = authed_client
    statuses = []
    for _ in range(31):
        response = await client.post(
            "/api/kb/search", json={"query": "адрес"}, headers=headers
        )
        statuses.append(response.status_code)
    assert statuses[:30] == [200] * 30
    assert statuses[30] == 429
    assert "подождите" in (
        await client.post("/api/kb/search", json={"query": "адрес"}, headers=headers)
    ).json()["detail"]


async def test_kb_import_is_idempotent_and_creates_drafts(db_engine) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        first = await import_savvi_export(db, EXPORT)
    async with maker() as db, db.begin():
        second = await import_savvi_export(db, EXPORT)

    assert len(first.created) == 9  # all mapped Savvi blocks
    assert second.created == []
    assert len(second.skipped_existing) == 9

    async with maker() as db:
        documents = (await db.execute(select(KbDocument))).scalars().all()
    assert len(documents) == 9
    assert all(d.status.value == "draft" for d in documents)
    assert all(d.index_state == "not_indexed" for d in documents)
    # The block Savvi kept switched off stays out of retrieval.
    promo = next(d for d in documents if d.category == "promo")
    assert promo.is_searchable is False
    # Content was normalized on the way in.
    gym = next(d for d in documents if d.title == "Тренажёрный зал: абонементы")
    assert "+7 777 400 56 76" in gym.body
    assert "\\-" not in gym.body


async def test_kb_import_preserves_owner_edits(db_engine, authed_client) -> None:
    """A document the owner already edited must never be overwritten."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        await import_savvi_export(db, EXPORT)
    async with maker() as db, db.begin():
        document = (
            await db.execute(
                select(KbDocument).where(KbDocument.title == "Об организации")
            )
        ).scalar_one()
        document.body = "ОТРЕДАКТИРОВАНО ВЛАДЕЛЬЦЕМ"
        document.version = 2

    async with maker() as db, db.begin():
        await import_savvi_export(db, EXPORT)

    async with maker() as db:
        document = (
            await db.execute(
                select(KbDocument).where(KbDocument.title == "Об организации")
            )
        ).scalar_one()
    assert document.body == "ОТРЕДАКТИРОВАНО ВЛАДЕЛЬЦЕМ"


async def test_faq_table_starts_empty(db_engine) -> None:
    """SPEC §11: no seeded FAQ texts in code, migrations or scripts."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        assert (await db.execute(select(KbFaq))).scalars().all() == []
