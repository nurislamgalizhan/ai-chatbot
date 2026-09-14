"""Chat manager API. Rendering history NEVER calls OpenAI (SPEC invariant 16)."""

from __future__ import annotations

import asyncio
import base64
import binascii
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.media import storage
from app.models import (
    AdminUser,
    AiCall,
    Chat,
    ChatModeHistory,
    ChatTag,
    InternalNote,
    KbChunk,
    Message,
    Tag,
)
from app.models.enums import AttentionClearedReason, AttentionReason, ChatMode
from app.schemas.chats import (
    AiCallDetail,
    AiCallDetailResponse,
    AttachTagRequest,
    AttentionRequest,
    ChatDetailResponse,
    ChatListItem,
    ChatListResponse,
    MessageListResponse,
    MessageResponse,
    ModeHistoryItem,
    ModeHistoryResponse,
    NoteCreateRequest,
    NoteListResponse,
    NoteResponse,
    ResolveRequest,
    SendMessageRequest,
    SendMessageResponse,
    SetModeRequest,
    TagResponse,
)
from app.security.audit import record_audit
from app.security.deps import client_ip, get_app_settings, get_db, require_admin
from app.services.chat_modes import set_mode
from app.services.chats import clear_attention, send_panel_message, set_attention

router = APIRouter(prefix="/api/chats", tags=["chats"], dependencies=[Depends(require_admin)])

_PAGE_SIZE = 20
_MESSAGES_PAGE_SIZE = 50
#: A forged cursor with an id above int8 fails inside asyncpg with a 500;
#: bounding it here turns that into the 422 every other bad cursor gets.
_MAX_BIGINT = 2**63
# Served with a FIXED type from our own extension, never from the
# provider's mimeType: an "image" that is really SVG/HTML must not be
# rendered in the admin's origin. Anything unknown downloads as bytes.
_MEDIA_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".webm": "audio/webm",
}

NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Чат не найден")


def _encode_cursor(ts: datetime, row_id: int) -> str:
    raw = f"{ts.isoformat()}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, id_str = raw.split("|", 1)
        row_id = int(id_str)
        if not 0 < row_id < _MAX_BIGINT:
            raise ValueError("cursor id out of range")
        return datetime.fromisoformat(ts_str), row_id
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Неверный курсор"
        ) from exc


async def _get_chat(db: AsyncSession, chat_id: int) -> Chat:
    chat = await db.get(Chat, chat_id)
    if chat is None:
        raise NOT_FOUND
    return chat


async def _detail_response(db: AsyncSession, chat: Chat) -> ChatDetailResponse:
    tags_map = await _tags_by_chat(db, [chat.id])
    return ChatDetailResponse(
        id=chat.id,
        phone=chat.phone,
        display_name=chat.display_name,
        wa_chat_id=chat.wa_chat_id,
        mode=chat.mode.value,
        paused_until=chat.paused_until,
        is_resolved=chat.is_resolved,
        needs_attention=chat.needs_attention,
        attention_reason=chat.attention_reason.value if chat.attention_reason else None,
        last_message_at=chat.last_message_at,
        total_cost_usd=float(chat.total_cost_usd),
        created_at=chat.created_at,
        tags=tags_map.get(chat.id, []),
    )


async def _tags_by_chat(db: AsyncSession, chat_ids: list[int]) -> dict[int, list[TagResponse]]:
    if not chat_ids:
        return {}
    rows = (
        await db.execute(
            select(ChatTag.chat_id, Tag)
            .join(Tag, Tag.id == ChatTag.tag_id)
            .where(ChatTag.chat_id.in_(chat_ids))
            .order_by(Tag.name)
        )
    ).all()
    result: dict[int, list[TagResponse]] = {}
    for chat_id, tag in rows:
        result.setdefault(chat_id, []).append(
            TagResponse(id=tag.id, name=tag.name, color=tag.color)
        )
    return result


@router.get("", response_model=ChatListResponse)
async def list_chats(  # noqa: PLR0912 - filter assembly
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    needs_attention: bool | None = None,
    is_resolved: bool | None = None,
    unread_only: bool = False,
    tag_id: int | None = None,
    mode: ChatMode | None = None,
) -> ChatListResponse:
    last_message = (
        select(func.left(func.coalesce(Message.body, Message.transcript, ""), 120))
        .where(Message.chat_id == Chat.id)
        .order_by(Message.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    stmt = select(Chat, last_message.label("preview"))

    if q:
        pattern = f"%{q}%"
        text_match = (
            select(Message.id)
            .where(Message.chat_id == Chat.id, Message.body.ilike(pattern))
            .limit(1)
            .exists()
        )
        stmt = stmt.where(
            or_(Chat.phone.ilike(pattern), Chat.display_name.ilike(pattern), text_match)
        )
    if needs_attention is not None:
        stmt = stmt.where(Chat.needs_attention == needs_attention)
    if is_resolved is not None:
        stmt = stmt.where(Chat.is_resolved == is_resolved)
    if unread_only:
        stmt = stmt.where(Chat.unread_count > 0)
    if tag_id is not None:
        stmt = stmt.where(
            select(ChatTag.tag_id)
            .where(ChatTag.chat_id == Chat.id, ChatTag.tag_id == tag_id)
            .exists()
        )
    if mode is not None:
        stmt = stmt.where(Chat.mode == mode)
    if cursor:
        ts, row_id = _decode_cursor(cursor)
        stmt = stmt.where(func.row(Chat.last_message_at, Chat.id) < func.row(ts, row_id))

    stmt = stmt.order_by(Chat.last_message_at.desc(), Chat.id.desc()).limit(_PAGE_SIZE + 1)
    rows = (await db.execute(stmt)).all()

    has_more = len(rows) > _PAGE_SIZE
    rows = rows[:_PAGE_SIZE]
    tags_map = await _tags_by_chat(db, [chat.id for chat, _ in rows])
    items = [
        ChatListItem(
            id=chat.id,
            phone=chat.phone,
            display_name=chat.display_name,
            mode=chat.mode.value,
            paused_until=chat.paused_until,
            is_resolved=chat.is_resolved,
            unread_count=chat.unread_count,
            needs_attention=chat.needs_attention,
            attention_reason=chat.attention_reason.value if chat.attention_reason else None,
            last_message_at=chat.last_message_at,
            last_actor=chat.last_actor.value if chat.last_actor else None,
            last_message_preview=preview or None,
            total_cost_usd=float(chat.total_cost_usd),
            tags=tags_map.get(chat.id, []),
        )
        for chat, preview in rows
    ]
    next_cursor = None
    if has_more and rows:
        last_chat = rows[-1][0]
        next_cursor = _encode_cursor(last_chat.last_message_at, last_chat.id)
    return ChatListResponse(items=items, next_cursor=next_cursor)


@router.get("/{chat_id}", response_model=ChatDetailResponse)
async def get_chat(
    chat_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ChatDetailResponse:
    """Read-only. Marking the dialog as opened is a separate POST: a GET
    is exempt from the CSRF/Origin checks, and a cross-site top-level
    link to this URL would otherwise clear «требует внимания» on a chat
    a customer is still waiting in."""
    chat = await _get_chat(db, chat_id)
    return await _detail_response(db, chat)


@router.post("/{chat_id}/open", response_model=ChatDetailResponse)
async def open_chat(
    chat_id: int,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ChatDetailResponse:
    """Opening a dialog in the panel marks it read and clears "needs
    attention" (owner decision) — the clearing is audited."""
    chat = await _get_chat(db, chat_id)
    chat.unread_count = 0
    await clear_attention(
        db,
        chat,
        AttentionClearedReason.PANEL_OPENED,
        actor_user_id=user.id,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return await _detail_response(db, chat)


@router.get("/{chat_id}/messages", response_model=MessageListResponse)
async def list_messages(
    chat_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    before_id: int | None = None,
) -> MessageListResponse:
    await _get_chat(db, chat_id)
    stmt = select(Message).where(Message.chat_id == chat_id)
    if before_id is not None:
        stmt = stmt.where(Message.id < before_id)
    stmt = stmt.order_by(Message.id.desc()).limit(_MESSAGES_PAGE_SIZE + 1)
    rows = list((await db.execute(stmt)).scalars().all())
    has_more = len(rows) > _MESSAGES_PAGE_SIZE
    rows = rows[:_MESSAGES_PAGE_SIZE]
    items = [
        MessageResponse(
            id=m.id,
            direction=m.direction.value,
            source=m.source.value,
            body=m.body,
            media_type=m.media_type.value if m.media_type else None,
            has_media=m.media_ref is not None,
            transcript=m.transcript,
            state=m.state.value,
            delivery_status=m.delivery_status,
            error_code=m.error_code,
            created_at=m.created_at,
            sent_at=m.sent_at,
        )
        for m in rows
    ]
    next_cursor = str(rows[-1].id) if has_more and rows else None
    return MessageListResponse(items=items, next_cursor=next_cursor)


@router.post("/{chat_id}/messages", response_model=SendMessageResponse)
async def send_message(
    chat_id: int,
    body: SendMessageRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SendMessageResponse:
    chat = await _get_chat(db, chat_id)
    message_id, created = await send_panel_message(
        db,
        chat,
        body.body,
        body.client_message_id,
        actor_user_id=user.id,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return SendMessageResponse(message_id=message_id, created=created)


@router.post("/{chat_id}/resolve", response_model=ChatDetailResponse)
async def resolve_chat(
    chat_id: int,
    body: ResolveRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ChatDetailResponse:
    chat = await _get_chat(db, chat_id)
    if chat.is_resolved != body.is_resolved:
        before = chat.is_resolved
        chat.is_resolved = body.is_resolved
        await record_audit(
            db,
            action="chat.resolved" if body.is_resolved else "chat.reopened",
            actor_user_id=user.id,
            entity_type="chat",
            entity_id=str(chat.id),
            before={"is_resolved": before},
            after={"is_resolved": body.is_resolved},
            ip=client_ip(request),
            correlation_id=getattr(request.state, "correlation_id", None),
        )
    return await _detail_response(db, chat)


@router.post("/{chat_id}/mode", response_model=ChatDetailResponse)
async def set_chat_mode(
    chat_id: int,
    body: SetModeRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ChatDetailResponse:
    """Manual mode control (SPEC §7): AUTO_AI / HUMAN / DISABLED, or a
    timed pause with the duration options from the UI."""
    chat = await _get_chat(db, chat_id)
    to_mode = ChatMode(body.mode)
    paused_until = None
    if to_mode is ChatMode.PAUSED_UNTIL:
        if body.pause_minutes is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Для паузы укажите длительность в минутах",
            )
        paused_until = datetime.now(UTC) + timedelta(minutes=body.pause_minutes)
    await set_mode(
        db,
        chat,
        to_mode,
        changed_by="admin",
        reason="manual",
        paused_until=paused_until,
        actor_user_id=user.id,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return await _detail_response(db, chat)


@router.get("/{chat_id}/mode-history", response_model=ModeHistoryResponse)
async def mode_history(
    chat_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> ModeHistoryResponse:
    await _get_chat(db, chat_id)
    rows = (
        (
            await db.execute(
                select(ChatModeHistory)
                .where(ChatModeHistory.chat_id == chat_id)
                .order_by(ChatModeHistory.id.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return ModeHistoryResponse(
        items=[
            ModeHistoryItem(
                from_mode=r.from_mode.value,
                to_mode=r.to_mode.value,
                by_admin=r.actor_user_id is not None,
                reason=r.reason,
                paused_until=r.paused_until,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )


@router.post("/{chat_id}/attention", status_code=status.HTTP_204_NO_CONTENT)
async def set_attention_flag(
    chat_id: int,
    body: AttentionRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    chat = await _get_chat(db, chat_id)
    correlation_id = getattr(request.state, "correlation_id", None)
    if body.needs_attention:
        await set_attention(db, chat, AttentionReason.MANUAL, correlation_id)
    else:
        await clear_attention(
            db,
            chat,
            AttentionClearedReason.MANUAL,
            actor_user_id=user.id,
            correlation_id=correlation_id,
        )


@router.get("/{chat_id}/notes", response_model=NoteListResponse)
async def list_notes(
    chat_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> NoteListResponse:
    await _get_chat(db, chat_id)
    rows = (
        (
            await db.execute(
                select(InternalNote)
                .where(InternalNote.chat_id == chat_id)
                .order_by(InternalNote.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return NoteListResponse(
        items=[NoteResponse(id=n.id, body=n.body, created_at=n.created_at) for n in rows]
    )


@router.post("/{chat_id}/notes", response_model=NoteResponse)
async def create_note(
    chat_id: int,
    body: NoteCreateRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> NoteResponse:
    await _get_chat(db, chat_id)
    note = InternalNote(chat_id=chat_id, user_id=user.id, body=body.body)
    db.add(note)
    await db.flush()
    await record_audit(
        db,
        action="chat.note_added",
        actor_user_id=user.id,
        entity_type="internal_note",
        entity_id=str(note.id),
        after={"chat_id": chat_id},
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return NoteResponse(id=note.id, body=note.body, created_at=note.created_at)


@router.post("/{chat_id}/tags", status_code=status.HTTP_204_NO_CONTENT)
async def attach_tag(
    chat_id: int,
    body: AttachTagRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await _get_chat(db, chat_id)
    tag = await db.get(Tag, body.tag_id)
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тег не найден")
    await db.execute(
        pg_insert(ChatTag).values(chat_id=chat_id, tag_id=body.tag_id).on_conflict_do_nothing()
    )
    await record_audit(
        db,
        action="chat.tag_attached",
        actor_user_id=user.id,
        entity_type="chat",
        entity_id=str(chat_id),
        after={"tag_id": body.tag_id},
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )


@router.delete("/{chat_id}/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def detach_tag(
    chat_id: int,
    tag_id: int,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await _get_chat(db, chat_id)
    row = (
        await db.execute(
            select(ChatTag).where(ChatTag.chat_id == chat_id, ChatTag.tag_id == tag_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тег не привязан к чату")
    await db.delete(row)
    await record_audit(
        db,
        action="chat.tag_detached",
        actor_user_id=user.id,
        entity_type="chat",
        entity_id=str(chat_id),
        after={"tag_id": tag_id},
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )


@router.get("/{chat_id}/messages/{message_id}/ai-call", response_model=AiCallDetailResponse)
async def message_ai_call(
    chat_id: int,
    message_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AiCallDetailResponse:
    """The "почему так" block: usage, cost, latency and the exact KB
    fragments behind one AI reply. Read-only — never calls OpenAI."""
    await _get_chat(db, chat_id)
    call = (
        await db.execute(
            select(AiCall)
            .where(AiCall.chat_id == chat_id, AiCall.message_id == message_id)
            .order_by(AiCall.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if call is None:
        # Honest empty state: FAQ/cache/template replies have no paid call.
        return AiCallDetailResponse(found=False)
    chunk_previews: list[dict[str, object]] = []
    if call.rag_chunk_ids:
        chunks = (
            (await db.execute(select(KbChunk).where(KbChunk.id.in_(list(call.rag_chunk_ids)))))
            .scalars()
            .all()
        )
        chunk_previews = [
            {
                "chunk_id": chunk.id,
                "title": (chunk.doc_metadata or {}).get("title"),
                "content": chunk.content[:400],
            }
            for chunk in chunks
        ]
    return AiCallDetailResponse(
        found=True,
        call=AiCallDetail(
            model=call.model,
            call_type=call.call_type.value,
            input_tokens=call.input_tokens,
            cached_input_tokens=call.cached_input_tokens,
            output_tokens=call.output_tokens,
            reasoning_tokens=call.reasoning_tokens,
            cost_usd=str(call.cost_usd),
            cost_kzt=str(call.cost_kzt),
            latency_ms=call.latency_ms,
            cache_hit=call.cache_hit,
            instruction_version=call.instruction_version,
            reasoning_effort=call.reasoning_effort,
            rag_chunks=chunk_previews,
            created_at=call.created_at,
        ),
    )


@router.get("/{chat_id}/messages/{message_id}/media")
async def message_media(
    chat_id: int,
    message_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Serve one stored media file to the panel.

    Both ids are resolved and matched, so a message id alone cannot be
    used to read another chat's file; the path comes from the database
    and is re-validated against the media root (no traversal, no
    symlinks out). Images were re-encoded to JPEG before being stored,
    and everything is served as an attachment with a fixed content type
    — a "photo" that was really SVG/HTML can never execute in the
    admin's origin.
    """
    await _get_chat(db, chat_id)
    message = (
        await db.execute(
            select(Message).where(Message.id == message_id, Message.chat_id == chat_id)
        )
    ).scalar_one_or_none()
    if message is None or not message.media_ref:
        # Also the retention case: the file is gone, the transcript stays.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл недоступен")
    settings = get_app_settings(request)
    try:
        data = await asyncio.to_thread(storage.read_bytes, settings.media_dir, message.media_ref)
    except storage.MediaStorageError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Файл недоступен"
        ) from None
    suffix = Path(message.media_ref).suffix.lower()
    content_type = _MEDIA_CONTENT_TYPES.get(suffix, "application/octet-stream")
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="media-{message_id}{suffix}"',
            "Cache-Control": "private, max-age=300",
        },
    )
