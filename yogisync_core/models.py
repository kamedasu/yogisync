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
    confidence: float = 1.0
    event_uid: str = ""
    gcal_event_id: Optional[str] = None
    time_unknown: bool = False

    def ensure_event_uid(self) -> str:
        if self.event_uid:
            return self.event_uid
        if self.time_unknown:
            date_key = self.date.date().isoformat()
        else:
            date_key = self.date.replace(second=0, microsecond=0).isoformat()
        title_key = self.title.strip()
        if self.location_name:
            location_key = self.location_name.strip()
            self.event_uid = f"{self.provider}:{date_key}:{title_key}:{location_key}"
        else:
            self.event_uid = f"{self.provider}:{date_key}:{title_key}"
        return self.event_uid

    def content_hash(self) -> str:
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


class SyncResult(BaseModel):
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
