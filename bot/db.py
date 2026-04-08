from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import asyncpg


@dataclass(slots=True)
class SessionRecord:
    id: int
    user_id: int
    title: str
    is_saved: bool
    badge_sent: bool
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class MessageRecord:
    role: str
    content_type: str
    content_text: str
    created_at: datetime


class Database:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=10,
            command_timeout=30,  # fail fast if a query hangs
        )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")
        return self._pool

    async def init_schema(self) -> None:
        query = """
        CREATE TABLE IF NOT EXISTS users (
            telegram_user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            active_session_id BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id BIGSERIAL PRIMARY KEY,
            telegram_user_id BIGINT NOT NULL REFERENCES users(telegram_user_id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT 'Новый диалог',
            is_saved BOOLEAN NOT NULL DEFAULT FALSE,
            badge_sent BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS messages (
            id BIGSERIAL PRIMARY KEY,
            session_id BIGINT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content_type TEXT NOT NULL,
            content_text TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
        ON sessions (telegram_user_id, updated_at DESC);

        CREATE INDEX IF NOT EXISTS idx_messages_session_created
        ON messages (session_id, created_at ASC);
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query)
            # Backward-compatible additive migration for existing databases.
            await conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS active_session_id BIGINT;"
            )
            await conn.execute(
                "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS badge_sent BOOLEAN NOT NULL DEFAULT FALSE;"
            )

    async def upsert_user(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> None:
        query = """
        INSERT INTO users (telegram_user_id, username, first_name, last_name)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (telegram_user_id) DO UPDATE
        SET username = EXCLUDED.username,
            first_name = EXCLUDED.first_name,
            last_name = EXCLUDED.last_name,
            updated_at = NOW();
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                query,
                telegram_user_id,
                username,
                first_name,
                last_name,
            )

    async def create_session(self, telegram_user_id: int) -> SessionRecord:
        query = """
        INSERT INTO sessions (telegram_user_id)
        VALUES ($1)
        RETURNING id, telegram_user_id, title, is_saved, badge_sent, created_at, updated_at;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, telegram_user_id)
        assert row is not None
        return SessionRecord(
            id=row["id"],
            user_id=row["telegram_user_id"],
            title=row["title"],
            is_saved=row["is_saved"],
            badge_sent=row["badge_sent"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_session(self, telegram_user_id: int, session_id: int) -> SessionRecord | None:
        query = """
        SELECT id, telegram_user_id, title, is_saved, badge_sent, created_at, updated_at
        FROM sessions
        WHERE id = $1 AND telegram_user_id = $2;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, session_id, telegram_user_id)
        if row is None:
            return None
        return SessionRecord(
            id=row["id"],
            user_id=row["telegram_user_id"],
            title=row["title"],
            is_saved=row["is_saved"],
            badge_sent=row["badge_sent"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_active_session(self, telegram_user_id: int) -> SessionRecord | None:
        query = """
        SELECT s.id, s.telegram_user_id, s.title, s.is_saved, s.badge_sent, s.created_at, s.updated_at
        FROM users u
        JOIN sessions s ON s.id = u.active_session_id
        WHERE u.telegram_user_id = $1 AND s.telegram_user_id = $1;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, telegram_user_id)
        if row is None:
            return None
        return SessionRecord(
            id=row["id"],
            user_id=row["telegram_user_id"],
            title=row["title"],
            is_saved=row["is_saved"],
            badge_sent=row["badge_sent"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def set_active_session(self, telegram_user_id: int, session_id: int) -> None:
        query = """
        UPDATE users
        SET active_session_id = $2, updated_at = NOW()
        WHERE telegram_user_id = $1;
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, telegram_user_id, session_id)

    async def create_and_activate_session(self, telegram_user_id: int) -> SessionRecord:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO sessions (telegram_user_id)
                    VALUES ($1)
                    RETURNING id, telegram_user_id, title, is_saved, badge_sent, created_at, updated_at;
                    """,
                    telegram_user_id,
                )
                assert row is not None
                await conn.execute(
                    """
                    UPDATE users
                    SET active_session_id = $2, updated_at = NOW()
                    WHERE telegram_user_id = $1;
                    """,
                    telegram_user_id,
                    row["id"],
                )
        return SessionRecord(
            id=row["id"],
            user_id=row["telegram_user_id"],
            title=row["title"],
            is_saved=row["is_saved"],
            badge_sent=row["badge_sent"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def add_message(
        self,
        session_id: int,
        role: str,
        content_type: str,
        content_text: str,
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO messages (session_id, role, content_type, content_text) VALUES ($1, $2, $3, $4);",
                    session_id,
                    role,
                    content_type,
                    content_text,
                )
                await conn.execute(
                    "UPDATE sessions SET updated_at = NOW() WHERE id = $1;",
                    session_id,
                )

    async def mark_badge_sent(self, session_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET badge_sent = TRUE WHERE id = $1;",
                session_id,
            )

    async def ensure_session_title(self, session_id: int, fallback_text: str) -> None:
        query = """
        UPDATE sessions
        SET title = LEFT($2, 120)
        WHERE id = $1 AND title = 'Новый диалог';
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, session_id, fallback_text.strip() or "Новый диалог")

    async def get_messages(self, session_id: int, limit: int = 20) -> list[MessageRecord]:
        query = """
        SELECT role, content_type, content_text, created_at
        FROM messages
        WHERE session_id = $1
        ORDER BY created_at DESC
        LIMIT $2;
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, session_id, limit)
        rows = list(reversed(rows))
        return [
            MessageRecord(
                role=row["role"],
                content_type=row["content_type"],
                content_text=row["content_text"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def set_saved(self, telegram_user_id: int, session_id: int, value: bool) -> bool:
        query = """
        UPDATE sessions
        SET is_saved = $3, updated_at = NOW()
        WHERE id = $1 AND telegram_user_id = $2
        RETURNING id;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, session_id, telegram_user_id, value)
        return row is not None

    async def delete_session(self, telegram_user_id: int, session_id: int) -> bool:
        query = """
        DELETE FROM sessions
        WHERE id = $1 AND telegram_user_id = $2
        RETURNING id;
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(query, session_id, telegram_user_id)
                if row is not None:
                    await conn.execute(
                        """
                        UPDATE users
                        SET active_session_id = NULL, updated_at = NOW()
                        WHERE telegram_user_id = $1 AND active_session_id = $2;
                        """,
                        telegram_user_id,
                        session_id,
                    )
        return row is not None

    async def list_recent_sessions(
        self,
        telegram_user_id: int,
        limit: int,
    ) -> list[SessionRecord]:
        query = """
        SELECT id, telegram_user_id, title, is_saved, badge_sent, created_at, updated_at
        FROM sessions
        WHERE telegram_user_id = $1
        ORDER BY updated_at DESC
        LIMIT $2;
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, telegram_user_id, limit)
        return [
            SessionRecord(
                id=row["id"],
                user_id=row["telegram_user_id"],
                title=row["title"],
                is_saved=row["is_saved"],
                badge_sent=row["badge_sent"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def list_saved_sessions(
        self,
        telegram_user_id: int,
        limit: int,
    ) -> list[SessionRecord]:
        query = """
        SELECT id, telegram_user_id, title, is_saved, badge_sent, created_at, updated_at
        FROM sessions
        WHERE telegram_user_id = $1 AND is_saved = TRUE
        ORDER BY updated_at DESC
        LIMIT $2;
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, telegram_user_id, limit)
        return [
            SessionRecord(
                id=row["id"],
                user_id=row["telegram_user_id"],
                title=row["title"],
                is_saved=row["is_saved"],
                badge_sent=row["badge_sent"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def trim_recent_sessions(self, telegram_user_id: int, keep: int) -> None:
        # We keep all saved sessions untouched and never auto-delete active dialog.
        query = """
        WITH active AS (
            SELECT active_session_id FROM users WHERE telegram_user_id = $1
        ),
        ranked AS (
            SELECT id, ROW_NUMBER() OVER (ORDER BY updated_at DESC) AS rn
            FROM sessions
            WHERE telegram_user_id = $1
              AND is_saved = FALSE
              AND id <> COALESCE((SELECT active_session_id FROM active), -1)
        )
        DELETE FROM sessions
        WHERE id IN (
            SELECT id FROM ranked WHERE rn > $2
        );
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, telegram_user_id, keep)

    async def ensure_active_session(self, telegram_user_id: int) -> SessionRecord:
        active = await self.get_active_session(telegram_user_id)
        if active is not None:
            return active
        return await self.create_and_activate_session(telegram_user_id)

    async def trim_saved_sessions(self, telegram_user_id: int, keep: int) -> None:
        query = """
        WITH active AS (
            SELECT active_session_id FROM users WHERE telegram_user_id = $1
        ),
        ranked AS (
            SELECT id, ROW_NUMBER() OVER (ORDER BY updated_at DESC) AS rn
            FROM sessions
            WHERE telegram_user_id = $1
              AND is_saved = TRUE
              AND id <> COALESCE((SELECT active_session_id FROM active), -1)
        )
        DELETE FROM sessions
        WHERE id IN (
            SELECT id FROM ranked WHERE rn > $2
        );
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, telegram_user_id, keep)

