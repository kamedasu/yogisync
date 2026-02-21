from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Optional, Tuple

from .models import Event


@dataclass
class StoreAction:
    action: str  # "created" | "updated" | "skipped"
    gcal_event_id: Optional[str]


class EventStore:
    def __init__(self, sqlite_path: str):
        self.conn = sqlite3.connect(sqlite_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self):
        self.conn.close()

    def _init_schema(self):
        cur = self.conn.cursor()

        # まず最低限テーブル作成（新規DB向け）
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_uid TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                title TEXT NOT NULL,
                date TEXT NOT NULL,
                location_name TEXT,
                address TEXT,
                instructor TEXT,
                reservation_id TEXT,
                source_url TEXT,
                base_price TEXT,
                option_menus TEXT,
                ticket_types TEXT,
                confidence REAL NOT NULL,
                time_unknown INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                gcal_event_id TEXT
            )
            """
        )
        self.conn.commit()

        # 既存DB向け：足りない列だけ追加（安全）
        cur.execute("PRAGMA table_info(events)")
        existing_cols = {row["name"] for row in cur.fetchall()}

        def add_col(name: str, ddl: str):
            if name in existing_cols:
                return
            cur.execute(f"ALTER TABLE events ADD COLUMN {name} {ddl}")

        add_col("provider", "TEXT")
        add_col("title", "TEXT")
        add_col("date", "TEXT")
        add_col("location_name", "TEXT")
        add_col("address", "TEXT")
        add_col("instructor", "TEXT")
        add_col("reservation_id", "TEXT")
        add_col("source_url", "TEXT")

        # NEW/既存拡張
        add_col("base_price", "TEXT")
        add_col("option_menus", "TEXT")
        add_col("ticket_types", "TEXT")

        add_col("confidence", "REAL")
        add_col("time_unknown", "INTEGER")
        add_col("content_hash", "TEXT")
        add_col("gcal_event_id", "TEXT")

        self.conn.commit()

    def _serialize_list(self, xs: Optional[list[str]]) -> Optional[str]:
        if xs is None:
            return None
        return json.dumps(xs, ensure_ascii=False)

    def _deserialize_list(self, s: Optional[str]) -> Optional[list[str]]:
        if not s:
            return None
        try:
            v = json.loads(s)
            if isinstance(v, list):
                return [str(x) for x in v]
        except Exception:
            pass
        return None

    def get_by_uid(self, event_uid: str) -> Optional[sqlite3.Row]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM events WHERE event_uid = ?", (event_uid,))
        return cur.fetchone()

    def upsert_event(self, event: Event) -> Tuple[str, Optional[str]]:
        """
        DBへ upsert する。
        戻り値: (action, gcal_event_id)
          action: "created" | "updated" | "skipped"
        """
        uid = event.ensure_event_uid()
        new_hash = event.content_hash()

        existing = self.get_by_uid(uid)
        cur = self.conn.cursor()

        option_menus_json = self._serialize_list(event.option_menus)
        ticket_types_json = self._serialize_list(event.ticket_types)

        if not existing:
            cur.execute(
                """
                INSERT INTO events (
                    event_uid, provider, title, date,
                    location_name, address, instructor,
                    reservation_id, source_url,
                    base_price, option_menus, ticket_types,
                    confidence, time_unknown, content_hash, gcal_event_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    event.provider,
                    event.title,
                    event.date.isoformat(),
                    event.location_name,
                    event.address,
                    event.instructor,
                    event.reservation_id,
                    event.source_url,
                    event.base_price,
                    option_menus_json,
                    ticket_types_json,
                    float(event.confidence),
                    1 if event.time_unknown else 0,
                    new_hash,
                    event.gcal_event_id,
                ),
            )
            self.conn.commit()
            return "created", event.gcal_event_id

        # 既存がある：hash比較して差分なければスキップ
        old_hash = existing["content_hash"] if "content_hash" in existing.keys() else None
        old_gcal = existing["gcal_event_id"] if "gcal_event_id" in existing.keys() else None

        if old_hash == new_hash:
            # 変化なし
            return "skipped", old_gcal

        # 差分あり：更新
        cur.execute(
            """
            UPDATE events SET
                provider = ?,
                title = ?,
                date = ?,
                location_name = ?,
                address = ?,
                instructor = ?,
                reservation_id = ?,
                source_url = ?,
                base_price = ?,
                option_menus = ?,
                ticket_types = ?,
                confidence = ?,
                time_unknown = ?,
                content_hash = ?,
                gcal_event_id = COALESCE(gcal_event_id, ?)
            WHERE event_uid = ?
            """,
            (
                event.provider,
                event.title,
                event.date.isoformat(),
                event.location_name,
                event.address,
                event.instructor,
                event.reservation_id,
                event.source_url,
                event.base_price,
                option_menus_json,
                ticket_types_json,
                float(event.confidence),
                1 if event.time_unknown else 0,
                new_hash,
                event.gcal_event_id,
                uid,
            ),
        )
        self.conn.commit()

        # gcal_event_id は既存優先で残す（なければ新しい値）
        cur.execute("SELECT gcal_event_id FROM events WHERE event_uid = ?", (uid,))
        row = cur.fetchone()
        gcal_event_id = row["gcal_event_id"] if row else old_gcal
        return "updated", gcal_event_id

    def update_gcal_event_id(self, event_uid: str, gcal_event_id: str) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE events SET gcal_event_id = ? WHERE event_uid = ?",
            (gcal_event_id, event_uid),
        )
        self.conn.commit()
