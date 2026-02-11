from __future__ import annotations

import base64
from typing import Dict, List, Optional, Tuple

from googleapiclient.discovery import build

from .auth import get_credentials
from .config import Config
from .models import GmailMessage

SCOPES_GMAIL = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]

def _decode_body(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_parts(payload: Dict) -> Tuple[Optional[str], Optional[str]]:
    text_plain = None
    text_html = None

    def walk(part: Dict) -> None:
        nonlocal text_plain, text_html
        mime_type = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if mime_type == "text/plain" and data and text_plain is None:
            text_plain = _decode_body(data)
        elif mime_type == "text/html" and data and text_html is None:
            text_html = _decode_body(data)

        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    return text_plain, text_html


def _parse_headers(headers: List[Dict]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for h in headers:
        name = h.get("name", "")
        value = h.get("value", "")
        if name:
            result[name.lower()] = value
    return result


def get_gmail_service(config: Config):
    creds = get_credentials(SCOPES_GMAIL, config.google_client_secret_path, config.google_token_path)
    return build("gmail", "v1", credentials=creds)


def fetch_messages(config: Config, limit: int = 50) -> List[GmailMessage]:
    service = get_gmail_service(config)
    user_id = "me"
    query = config.gmail_query

    messages: List[GmailMessage] = []
    page_token = None
    fetched = 0

    while True:
        req = service.users().messages().list(userId=user_id, q=query, maxResults=min(500, limit - fetched), pageToken=page_token)
        resp = req.execute()
        for msg in resp.get("messages", []) or []:
            msg_id = msg.get("id")
            if not msg_id:
                continue
            full = service.users().messages().get(userId=user_id, id=msg_id, format="full").execute()
            payload = full.get("payload", {})
            headers = _parse_headers(payload.get("headers", []) or [])
            text_plain, text_html = _extract_parts(payload)
            messages.append(
                GmailMessage(
                    id=msg_id,
                    thread_id=full.get("threadId"),
                    subject=headers.get("subject"),
                    from_email=headers.get("from"),
                    snippet=full.get("snippet"),
                    text_plain=text_plain,
                    text_html=text_html,
                )
            )
            fetched += 1
            if fetched >= limit:
                return messages

        page_token = resp.get("nextPageToken")
        if not page_token or fetched >= limit:
            break

    return messages
