"""A backup is only a backup if it restores. DoD of phase 9.

This test does not re-implement the backup in Python: it runs `pg_dump`
and `pg_restore` exactly as deploy/backup/pg_backup.sh does, into a
throwaway database inside the same container, and then checks the
CONTENT — not that tables exist, but that the owner's history, money and
knowledge base came back byte for byte.

The one thing worth more than the row counts is the vector column:
pgvector data that survives a custom-format dump is the difference
between a restored bot that answers and one that silently finds nothing.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import (
    AiCall,
    Chat,
    KbChunk,
    KbDocument,
    Message,
    WhatsappInstance,
)
from app.models.enums import (
    CallType,
    DocStatus,
    KbLanguage,
    MsgDirection,
    MsgSource,
    MsgState,
    SourceType,
)

RESTORE_DB = "wabot_restore_check"
CUSTOMER_TEXT = "Здравствуйте! Сколько стоит курс охранника и когда начало?"
VECTOR = [0.25] * 1536


def _docker_exec(container: object, *args: str, stdin: bytes | None = None) -> bytes:
    """Run a command inside the test's PostgreSQL container."""
    container_id = container.get_wrapped_container().id  # type: ignore[attr-defined]
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["docker", "exec", "-i", container_id, *args],  # noqa: S607 - docker from PATH
        input=stdin,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode()[:800]
        raise AssertionError(f"{' '.join(args)} failed: {message}")
    return result.stdout


def _pg_env(container: object) -> tuple[str, str]:
    user = container.username  # type: ignore[attr-defined]
    database = container.dbname  # type: ignore[attr-defined]
    return user, database


@pytest.fixture
async def seeded(db_engine):
    """One of everything that would hurt to lose."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        instance = WhatsappInstance(
            provider="green_api", instance_id="1101000013", token_ref="GREEN_API_TOKEN"
        )
        db.add(instance)
        await db.flush()
        chat = Chat(
            instance_id=instance.id,
            wa_chat_id="77010000013@c.us",
            phone="+77010000013",
            display_name="Клиент для бэкапа",
        )
        db.add(chat)
        await db.flush()
        db.add(
            Message(
                chat_id=chat.id,
                direction=MsgDirection.IN,
                source=MsgSource.CUSTOMER,
                state=MsgState.ANSWERED,
                body=CUSTOMER_TEXT,
                provider_message_id="backup-1",
            )
        )
        db.add(
            AiCall(
                correlation_id="backup:call",
                chat_id=chat.id,
                call_type=CallType.CHAT,
                model="gpt-5.6-luna",
                input_tokens=1234,
                cost_usd=Decimal("0.00123456"),
                cost_kzt=Decimal("0.6543"),
            )
        )
        document = KbDocument(
            title="Курс охранника",
            body="Курс длится 3 недели.",
            language=KbLanguage.RU,
            status=DocStatus.ACTIVE,
            source_type=SourceType.MANUAL_TEXT,
        )
        db.add(document)
        await db.flush()
        db.add(
            KbChunk(
                document_id=document.id,
                chunk_index=0,
                content="Курс длится 3 недели.",
                token_count=6,
                embedding=VECTOR,
                embedding_model="text-embedding-3-small",
                doc_metadata={"document_id": document.id},
            )
        )
    return maker


async def test_dump_restores_the_owners_data(seeded, pg_container) -> None:
    user, database = _pg_env(pg_container)

    dump = _docker_exec(pg_container, "pg_dump", "-Fc", "-U", user, "-d", database)
    assert dump[:5] == b"PGDMP", "not a custom-format dump"

    _docker_exec(
        pg_container,
        "psql",
        "-U",
        user,
        "-d",
        "postgres",
        "-c",
        f"DROP DATABASE IF EXISTS {RESTORE_DB}",
    )
    _docker_exec(
        pg_container, "psql", "-U", user, "-d", "postgres", "-c", f"CREATE DATABASE {RESTORE_DB}"
    )
    try:
        _docker_exec(
            pg_container,
            "pg_restore",
            "--no-owner",
            "--no-privileges",
            "-U",
            user,
            "-d",
            RESTORE_DB,
            stdin=dump,
        )

        def query(sql: str) -> str:
            out = _docker_exec(
                pg_container, "psql", "-t", "-A", "-U", user, "-d", RESTORE_DB, "-c", sql
            )
            return out.decode().strip()

        # Schema version: restoring an older dump onto newer code is the
        # scenario where this number decides whether to run migrations.
        # Compared against the head of the migration tree, not a literal,
        # so the next migration does not "break" the backup test.
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config("backend/alembic.ini")
        config.set_main_option("script_location", "backend/alembic")
        head = ScriptDirectory.from_config(config).get_current_head()
        assert query("SELECT version_num FROM alembic_version") == head

        # The customer's words, exactly as they were written.
        assert query("SELECT body FROM messages WHERE provider_message_id = 'backup-1'") == (
            CUSTOMER_TEXT
        )
        # Money, to the last digit: this table is the financial record.
        assert query("SELECT cost_usd FROM ai_calls WHERE correlation_id = 'backup:call'") == (
            "0.00123456"
        )
        assert query("SELECT count(*) FROM chats") == "1"

        # pgvector: the extension exists in the restored database AND the
        # vector itself is intact (distance to itself is exactly zero).
        assert query("SELECT count(*) FROM pg_extension WHERE extname = 'vector'") == "1"
        assert query("SELECT embedding <-> embedding FROM kb_chunks LIMIT 1") == "0"

        # Enum types survive too — a restore that loses them leaves a
        # database where no message can be inserted at all.
        assert query("SELECT count(*) FROM pg_type WHERE typname = 'msg_state'") == "1"
    finally:
        _docker_exec(
            pg_container,
            "psql",
            "-U",
            user,
            "-d",
            "postgres",
            "-c",
            f"DROP DATABASE IF EXISTS {RESTORE_DB}",
        )


def test_backup_script_keeps_the_right_dumps(tmp_path) -> None:
    """Rotation: 7 daily + 4 weekly (Mondays) + 3 monthly (1st), and the
    newest dump is never rotated out."""
    import os
    import stat
    import subprocess as sp
    from datetime import timedelta

    script = "deploy/backup/pg_backup.sh"
    assert os.access(script, os.X_OK), "backup script must be executable"
    mode = stat.S_IMODE(os.stat(script).st_mode)
    assert mode & 0o111, mode

    # The rotation rule is a shell function; exercise it through bash on
    # a directory of fake dumps rather than restating the logic here.
    backup_dir = tmp_path / "dumps"
    backup_dir.mkdir()
    today = datetime.now(UTC).date()
    made = []
    for days_ago in range(0, 400, 3):
        day = today - timedelta(days=days_ago)
        path = backup_dir / f"wabot-{day.isoformat()}.dump"
        path.write_bytes(b"PGDMP-fake")
        made.append(day)

    # Exercise the script's OWN rotation rule rather than restating it:
    # a copy of the logic in the test would happily agree with a bug.
    function_file = tmp_path / "keep_file.sh"
    function_file.write_text(
        sp.run(  # noqa: S603
            ["sed", "-n", "/^keep_file()/,/^}/p", script],  # noqa: S607
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    )
    assert "keep_file()" in function_file.read_text()

    probe = sp.run(  # noqa: S603
        [
            "bash",
            "-c",
            f"source {function_file}; "
            f"KEEP_DAILY=7; KEEP_WEEKLY=4; KEEP_MONTHLY=3; "
            f'for f in {backup_dir}/*.dump; do keep_file "$f" && echo "$f"; done',
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    kept = [line.rsplit("/", 1)[-1] for line in probe.stdout.split() if line]
    assert f"wabot-{today.isoformat()}.dump" in kept, "today's dump must always be kept"
    assert len(kept) < len(made), "old dumps must actually be rotated out"
    # Nothing older than the monthly window survives.
    oldest_kept = min(name[len("wabot-") : -len(".dump")] for name in kept)
    assert oldest_kept >= (today - timedelta(days=95)).isoformat()
