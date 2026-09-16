"""Chat manager API: pagination integrity, idempotent panel send,
attention side effects, tags, notes, 404 semantics."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import AuditLog, Chat, Message, WaOutbox, WhatsappInstance
from app.services.chats import upsert_chat_for_incoming
from tests.conftest import login


@pytest.fixture
async def instance_pk(db_engine) -> int:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        row = WhatsappInstance(
            provider="green_api", instance_id="1101000001", token_ref="GREEN_API_TOKEN"
        )
        db.add(row)
        await db.flush()
        return row.id


@pytest.fixture
async def authed(client, settings, admin_factory):
    admin = await admin_factory()
    csrf = await login(client, settings, admin)
    return client, {"X-CSRF-Token": csrf}


async def _make_chat(
    db_engine,
    instance_pk: int,
    phone_suffix: str,
    *,
    needs_attention: bool = False,
    last_message_at: datetime | None = None,
) -> int:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        chat = Chat(
            instance_id=instance_pk,
            wa_chat_id=f"7701{phone_suffix}@c.us",
            phone=f"+7701{phone_suffix}",
            display_name=f"Client {phone_suffix}",
            needs_attention=needs_attention,
            unread_count=2,
        )
        if last_message_at is not None:
            chat.last_message_at = last_message_at
        db.add(chat)
        await db.flush()
        return chat.id


async def test_pagination_neither_loses_nor_duplicates(authed, db_engine, instance_pk) -> None:
    client, _ = authed
    base = datetime.now(UTC)
    expected_ids = []
    for i in range(45):
        chat_id = await _make_chat(
            db_engine,
            instance_pk,
            f"{1000000 + i}",
            last_message_at=base - timedelta(minutes=i),
        )
        expected_ids.append(chat_id)

    seen: list[int] = []
    cursor: str | None = None
    for _ in range(10):
        url = "/api/chats" + (f"?cursor={cursor}" if cursor else "")
        page = (await client.get(url)).json()
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == len(set(seen)) == 45  # no duplicates, no losses
    assert set(seen) == set(expected_ids)
    assert seen == sorted(seen, key=expected_ids.index)  # newest first


async def test_filters(authed, db_engine, instance_pk) -> None:
    client, _ = authed
    await _make_chat(db_engine, instance_pk, "2000001", needs_attention=True)
    await _make_chat(db_engine, instance_pk, "2000002")

    flagged = (await client.get("/api/chats?needs_attention=true")).json()["items"]
    assert [c["phone"] for c in flagged] == ["+77012000001"]

    by_phone = (await client.get("/api/chats?q=2000002")).json()["items"]
    assert [c["phone"] for c in by_phone] == ["+77012000002"]


async def test_open_chat_clears_attention_and_unread_with_audit(
    authed, db_engine, instance_pk
) -> None:
    client, _ = authed
    chat_id = await _make_chat(db_engine, instance_pk, "3000001", needs_attention=True)

    # A plain GET is read-only: a cross-site link must not clear the flag.
    assert (await client.get(f"/api/chats/{chat_id}")).json()["needs_attention"] is True
    _, headers = authed
    response = await client.post(f"/api/chats/{chat_id}/open", headers=headers)
    assert response.status_code == 200
    assert response.json()["needs_attention"] is False

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        chat = await db.get(Chat, chat_id)
        audit = (
            await db.execute(select(AuditLog).where(AuditLog.action == "chat.attention_cleared"))
        ).scalar_one()
    assert chat.unread_count == 0
    assert chat.attention_cleared_reason == "panel_opened"
    assert audit.entity_id == str(chat_id)


async def test_panel_send_is_idempotent(authed, db_engine, instance_pk) -> None:
    client, headers = authed
    chat_id = await _make_chat(db_engine, instance_pk, "4000001")
    payload = {"body": "Добрый день!", "client_message_id": str(uuid.uuid4())}

    first = await client.post(f"/api/chats/{chat_id}/messages", json=payload, headers=headers)
    second = await client.post(f"/api/chats/{chat_id}/messages", json=payload, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert first.json()["message_id"] == second.json()["message_id"]

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        messages = (await db.execute(select(Message))).scalars().all()
        outbox = (await db.execute(select(WaOutbox))).scalars().all()
    assert len(messages) == 1
    assert messages[0].state.value == "GENERATED"  # persisted before sending
    assert len(outbox) == 1  # exactly one send queued


async def test_message_list_pagination(authed, db_engine, instance_pk) -> None:
    client, headers = authed
    chat_id = await _make_chat(db_engine, instance_pk, "5000001")
    for i in range(60):
        await client.post(
            f"/api/chats/{chat_id}/messages",
            json={"body": f"msg {i}", "client_message_id": str(uuid.uuid4())},
            headers=headers,
        )

    page1 = (await client.get(f"/api/chats/{chat_id}/messages")).json()
    assert len(page1["items"]) == 50
    assert page1["next_cursor"] is not None
    page2 = (
        await client.get(f"/api/chats/{chat_id}/messages?before_id={page1['next_cursor']}")
    ).json()
    assert len(page2["items"]) == 10
    all_ids = [m["id"] for m in page1["items"] + page2["items"]]
    assert len(all_ids) == len(set(all_ids)) == 60


async def test_resolve_and_notes_and_tags(authed, db_engine, instance_pk) -> None:
    client, headers = authed
    chat_id = await _make_chat(db_engine, instance_pk, "6000001")

    resolved = await client.post(
        f"/api/chats/{chat_id}/resolve", json={"is_resolved": True}, headers=headers
    )
    assert resolved.json()["is_resolved"] is True

    note = await client.post(
        f"/api/chats/{chat_id}/notes",
        json={"body": "перезвонить завтра"},
        headers=headers,
    )
    assert note.status_code == 200
    notes = (await client.get(f"/api/chats/{chat_id}/notes")).json()["items"]
    assert [n["body"] for n in notes] == ["перезвонить завтра"]

    tag = (await client.post("/api/tags", json={"name": "Аренда"}, headers=headers)).json()
    dup = await client.post("/api/tags", json={"name": "аренда"}, headers=headers)
    assert dup.status_code == 409  # case-insensitive uniqueness

    attach = await client.post(
        f"/api/chats/{chat_id}/tags", json={"tag_id": tag["id"]}, headers=headers
    )
    assert attach.status_code == 204
    detail = (await client.get(f"/api/chats/{chat_id}")).json()
    assert [t["name"] for t in detail["tags"]] == ["Аренда"]

    detach = await client.delete(f"/api/chats/{chat_id}/tags/{tag['id']}", headers=headers)
    assert detach.status_code == 204


async def test_missing_chat_is_404_not_500(authed) -> None:
    client, headers = authed
    assert (await client.get("/api/chats/999999")).status_code == 404
    assert (
        await client.post(
            "/api/chats/999999/messages",
            json={"body": "x", "client_message_id": str(uuid.uuid4())},
            headers=headers,
        )
    ).status_code == 404
    assert (await client.get("/api/chats/999999/messages")).status_code == 404


async def test_unread_count_survives_concurrent_increments(db_engine, instance_pk) -> None:
    """The unread increment is a single atomic UPSERT: N concurrent
    inbound messages -> unread_count == N, and exactly one chat row."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async def one_increment(i: int) -> None:
        async with maker() as db, db.begin():
            await upsert_chat_for_incoming(
                db, instance_pk, "77017000001@c.us", "+77017000001", f"Client {i}"
            )

    await asyncio.gather(*(one_increment(i) for i in range(12)))

    async with maker() as db:
        chats = (await db.execute(select(Chat))).scalars().all()
        count = (await db.execute(text("SELECT unread_count FROM chats"))).scalar_one()
    assert len(chats) == 1
    assert count == 12
