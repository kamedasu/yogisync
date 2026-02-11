from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional, Tuple

from .models import Event


class EventStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._ensure_table()

    def _ensure_table(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_uid TEXT PRIMARY KEY,
                provider TEXT,
                date TEXT,
                title TEXT,
                reservation_id TEXT,
                source_url TEXT,
                gcal_event_id TEXT,
                content_hash TEXT,
                updated_at TEXT
            )
            """
        )
        self.conn.commit()

    def get_event(self, event_uid: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM events WHERE event_uid = ?", (event_uid,))
        return cur.fetchone()

    def upsert_event(self, event: Event) -> Tuple[str, Optional[str]]:
        event_uid = event.ensure_event_uid()
        content_hash = event.content_hash()
        now = datetime.utcnow().isoformat()

        existing = self.get_event(event_uid)
        if existing:
            existing_gcal_event_id = existing["gcal_event_id"]
            has_gcal_id = bool(existing_gcal_event_id)  # None / "" を両方 false扱い

            # 内容が同じ＆GCal同期済みならスキップ
            if existing["content_hash"] == content_hash and has_gcal_id:
                return "skipped", existing_gcal_event_id

            # 内容が同じ＆GCal未同期なら、DB更新は不要だけど同期は走らせたい
            if existing["content_hash"] == content_hash and not has_gcal_id:
                return "updated", None

            # 内容が違う場合は UPDATE（gcal_event_id は保持）
            self.conn.execute(
                """
                UPDATE events
                SET provider = ?, date = ?, title = ?, reservation_id = ?, source_url = ?,
                    content_hash = ?, updated_at = ?
                WHERE event_uid = ?
                """,
                (
                    event.provider,
                    event.date.isoformat(),
                    event.title,
                    event.reservation_id,
                    event.source_url,
                    content_hash,
                    now,
                    event_uid,
                ),
            )
            self.conn.commit()
            return "updated", existing_gcal_event_id

        # existing が無い場合は INSERT
        self.conn.execute(
            """
            INSERT INTO events (
                event_uid, provider, date, title, reservation_id, source_url,
                gcal_event_id, content_hash, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_uid,
                event.provider,
                event.date.isoformat(),
                event.title,
                event.reservation_id,
                event.source_url,
                event.gcal_event_id,
                content_hash,
                now,
            ),
        )
        self.conn.commit()
        return "created", None

    def update_gcal_event_id(self, event_uid: str, gcal_event_id: str) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            "UPDATE events SET gcal_event_id = ?, updated_at = ? WHERE event_uid = ?",
            (gcal_event_id, now, event_uid),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
