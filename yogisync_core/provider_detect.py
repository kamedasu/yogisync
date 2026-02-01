from __future__ import annotations

from typing import Optional

from .models import GmailMessage, Provider


def _combine_text(msg: GmailMessage) -> str:
    parts = [msg.subject or "", msg.from_email or "", msg.text_plain or "", msg.text_html or "", msg.snippet or ""]
    return "\n".join(parts).lower()


def detect_provider(msg: GmailMessage) -> Optional[Provider]:
    text = _combine_text(msg)
    from_email = (msg.from_email or "").lower()
    subject = (msg.subject or "").lower()

    if "peatix" in from_email or "peatix.com" in text or "peatix" in subject:
        return "peatix"
    if "mosh" in from_email or "mosh.jp" in text or "mosh" in subject:
        return "mosh"
    if "bonne" in from_email or "スタジオbonne".lower() in text or "studio bonne" in text or "bonne" in subject:
        return "bonne"
    if "yes tokyo" in text or "yes-tokyo" in text or "yestokyo" in text or "yes tokyo" in subject:
        return "yes_tokyo"
    if "life tuning" in text or "life tuning days" in text or "lifetuning" in text:
        return "life_tuning"

    return None
