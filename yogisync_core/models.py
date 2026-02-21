from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


Provider = Literal["mosh", "peatix", "bonne", "yes_tokyo", "life_tuning"]


class Event(BaseModel):
    provider: Provider
    title: str
    date: datetime
    location_name: Optional[str] = None
    address: Optional[str] = None
    instructor: Optional[str] = None
    reservation_id: Optional[str] = None
    source_url: Optional[str] = None

    # --- NEW: pricing / options (MOSHなどで使う) ---
    base_price: Optional[str] = None                 # 例: "¥2500"
    option_menus: Optional[list[str]] = None         # 例: ["レンタルヨガマット×1(￥200)"]

    confidence: float = 1.0
    event_uid: str = ""
    gcal_event_id: Optional[str] = None
    time_unknown: bool = False

    def ensure_event_uid(self) -> str:
        """
        UID生成ルール（重複防止が最優先）:

        - 既に event_uid が入っているならそれを使う
        - provider=peatix は reservation_id（確認番号）を最優先:
            peatix:<reservation_id>
        - reservation_id が無い peatix は source_url を次点:
            peatix:url:<source_url>
          （※ title を混ぜない。title変更でUIDが変わって2件化するのを防ぐ）
        - それ以外/最終フォールバック:
            <provider>:<date_key>
          さらに不足時は content_hash の短縮を付与
        """
        if self.event_uid:
            return self.event_uid

        provider = self.provider

        # --- Peatixは確認番号を最優先（ユーザー要望） ---
        if provider == "peatix":
            if self.reservation_id:
                rid = str(self.reservation_id).strip()
                if rid:
                    self.event_uid = f"peatix:{rid}"
                    return self.event_uid

            if self.source_url:
                url = str(self.source_url).strip()
                if url:
                    self.event_uid = f"peatix:url:{url}"
                    return self.event_uid

            # 予約番号もURLも無い場合：日付キーのみ（titleは絶対混ぜない）
            if self.time_unknown:
                date_key = self.date.date().isoformat()
            else:
                date_key = self.date.replace(second=0, microsecond=0).isoformat()
            self.event_uid = f"peatix:{date_key}"
            return self.event_uid

        # --- 他providerの一般ルール ---
        if self.time_unknown:
            date_key = self.date.date().isoformat()
        else:
            date_key = self.date.replace(second=0, microsecond=0).isoformat()

        # title は基本入れない（変更でUIDが変わるのを避ける）
        if self.location_name:
            location_key = self.location_name.strip()
            base_uid = f"{provider}:{date_key}:{location_key}"
        else:
            base_uid = f"{provider}:{date_key}"

        # 衝突保険として hash を付ける
        short_hash = self.content_hash()[:12]
        self.event_uid = f"{base_uid}:{short_hash}"
        return self.event_uid

    def content_hash(self) -> str:
        options = self.option_menus or []
        options_key = "\n".join([str(x) for x in options])  # 順序維持（同じ並びなら同じhash）

        payload = "|".join(
            [
                self.provider,
                self.title,
                self.date.isoformat(),
                str(self.location_name or ""),
                str(self.address or ""),
                str(self.instructor or ""),
                str(self.reservation_id or ""),
                str(self.source_url or ""),
                str(self.base_price or ""),
                options_key,
                f"{self.confidence:.2f}",
                "1" if self.time_unknown else "0",
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class GmailMessage(BaseModel):
    id: str
    thread_id: Optional[str] = None
    subject: Optional[str] = None
    from_email: Optional[str] = None
    snippet: Optional[str] = None
    text_plain: Optional[str] = None
    text_html: Optional[str] = None


class YogaDecision(BaseModel):
    is_yoga: bool
    confidence: float
    reason: str
    matched_signals: list[str]


class SyncResult(BaseModel):
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
