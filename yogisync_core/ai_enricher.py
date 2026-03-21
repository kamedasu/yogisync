from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from pydantic import BaseModel, Field

from .config import Config
from .models import Event, GmailMessage

logger = logging.getLogger(__name__)


class EnrichResult(BaseModel):
    instructor: Optional[str] = None
    location_name: Optional[str] = None
    address: Optional[str] = None
    base_price: Optional[str] = None
    option_menus: Optional[list[str]] = None
    ticket_types: Optional[list[str]] = None
    notes: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


_ENRICH_JSON_SCHEMA: dict[str, Any] = {
    "name": "event_meta_enrich_result",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "instructor": {"type": ["string", "null"]},
            "location_name": {"type": ["string", "null"]},
            "address": {"type": ["string", "null"]},
            "base_price": {"type": ["string", "null"]},
            "option_menus": {"type": ["array", "null"], "items": {"type": "string"}},
            "ticket_types": {"type": ["array", "null"], "items": {"type": "string"}},
            "notes": {"type": ["string", "null"]},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "instructor",
            "location_name",
            "address",
            "base_price",
            "option_menus",
            "ticket_types",
            "notes",
            "confidence",
            "evidence",
        ],
    },
}
_TOOL_SCHEMA: dict[str, Any] = _ENRICH_JSON_SCHEMA["schema"]

_MISSING_TEXT_VALUES = {"", "-", "--", "n/a", "na", "none", "null", "未定", "不明"}

_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_INSTRUCTOR_KW_RE = re.compile(r"(インストラクター|講師)\s*[:：]\s*([^\n\r<]{1,120})")
# JSONっぽい断片のトリガ
_JSON_HINT_KW = ("description", "summary", "instructor", "instagram", "講師", "インストラクター")


def normalize_html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def _short_evidence(text: str) -> str:
    raw = str(text).strip()
    if not raw:
        return ""
    words = raw.split()
    if len(words) > 25:
        return " ".join(words[:25])
    return raw[:160]


def _extract_meta_hints(soup: BeautifulSoup) -> list[str]:
    hints: list[str] = []
    for key in ["description", "og:description", "twitter:description"]:
        tag = soup.find("meta", attrs={"name": key}) or soup.find("meta", attrs={"property": key})
        if tag and tag.get("content"):
            c = str(tag.get("content")).strip()
            if c:
                hints.append(f"[meta:{key}] {c[:300]}")
    for key in ["og:title", "twitter:title"]:
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if tag and tag.get("content"):
            c = str(tag.get("content")).strip()
            if c:
                hints.append(f"[meta:{key}] {c[:200]}")
    return hints[:8]


def _extract_ld_json(soup: BeautifulSoup) -> list[str]:
    """
    <script type="application/ld+json"> があると description が入っていることがある。
    """
    out: list[str] = []
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        s = sc.string
        if not s:
            continue
        s2 = s.strip()
        if not s2:
            continue
        # 長すぎるので上限
        out.append("[ld+json] " + s2[:4000])
        if len(out) >= 2:
            break
    return out


def _extract_script_snippets(soup: BeautifulSoup) -> list[str]:
    """
    JSアプリ系のページは本文がscript内JSONにあることがある。
    キーワードが含まれるscriptだけを短く切り出す。
    """
    snippets: list[str] = []
    for sc in soup.find_all("script"):
        s = sc.string
        if not s:
            continue
        s2 = s.strip()
        if not s2:
            continue
        low = s2.lower()
        if not any(k in low for k in _JSON_HINT_KW):
            continue

        # instructor行が入っていれば最優先
        m = _INSTRUCTOR_KW_RE.search(s2)
        if m:
            snippets.append(f"[script_instructor_hint] {m.group(0)[:200]}")
            break

        # instagram URL があれば拾う
        if "instagram.com" in low:
            for u in _URL_RE.findall(s2):
                if "instagram.com" in u:
                    snippets.append(f"[script_instagram] {u[:200]}")
                    break

        # JSON全体は重いので、descriptionっぽい周辺だけ薄く抜く
        if "description" in low or "summary" in low:
            idx = low.find("description")
            if idx < 0:
                idx = low.find("summary")
            if idx >= 0:
                start = max(0, idx - 800)
                end = min(len(s2), idx + 1800)
                snippets.append("[script_json_window] " + s2[start:end])
                if len(snippets) >= 2:
                    break

    # uniq
    uniq: list[str] = []
    seen = set()
    for x in snippets:
        if x in seen:
            continue
        seen.add(x)
        uniq.append(x)
    return uniq[:4]


def _extract_html_keyword_window(html: str) -> list[str]:
    """
    HTMLの生文字列にインストラクター等が居る場合、周辺だけ抜粋して渡す。
    （get_text で落ちる/消える場合の救済）
    """
    if not html:
        return []
    m = _INSTRUCTOR_KW_RE.search(html)
    if not m:
        return []
    idx = m.start()
    start = max(0, idx - 1200)
    end = min(len(html), idx + 1200)
    window = html[start:end]
    # タグだらけなので軽く整形
    window = re.sub(r"\s+", " ", window)
    return [f"[html_window] {window[:2500]}"]


def fetch_source_text(url: str, timeout_sec: int) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Referer": "https://peatix.com/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    with requests.Session() as session:
        session.headers.update(headers)
        res = session.get(url, timeout=timeout_sec, allow_redirects=True)

    res.raise_for_status()

    logger.info(
        "ai_enricher.fetch: encoding=%s apparent_encoding=%s url_final=%s",
        res.encoding,
        res.apparent_encoding,
        str(res.url),
    )

    if not res.encoding or res.encoding.lower() in {"iso-8859-1", "ascii"}:
        res.encoding = res.apparent_encoding

    html = res.text or ""

    logger.info(
        "ai_enricher.fetch: requests status=%s url_in=%s url_final=%s content_type=%s html_len=%s head=%s",
        res.status_code,
        url,
        str(res.url),
        res.headers.get("content-type"),
        len(html),
        (html[:600].replace("\n", " ") if html else ""),
    )

    html_has_instructor_kw = ("インストラクター" in html) or ("講師" in html)
    html_has_ricia = "リシア" in html
    html_has_nature_collective = ("Nature collective" in html) or ("Nature Collective" in html)
    html_has_instagram = "instagram.com" in html
    html_instructor_match = _INSTRUCTOR_KW_RE.search(html)

    logger.info(
        "ai_enricher.fetch: html_probe has_instructor_kw=%s has_ricia=%s has_nature_collective=%s has_instagram=%s instructor_match=%s",
        html_has_instructor_kw,
        html_has_ricia,
        html_has_nature_collective,
        html_has_instagram,
        (html_instructor_match.group(0)[:160] if html_instructor_match else ""),
    )

    soup = BeautifulSoup(html, "lxml")

    # まず「通常の可視テキスト」
    text = normalize_html_to_text(html)

    visible_has_instructor_kw = ("インストラクター" in text) or ("講師" in text)
    visible_has_ricia = "リシア" in text
    visible_has_nature_collective = ("Nature collective" in text) or ("Nature Collective" in text)

    logger.info(
        "ai_enricher.fetch: visible_text_probe text_len=%s has_instructor_kw=%s has_ricia=%s has_nature_collective=%s",
        len(text or ""),
        visible_has_instructor_kw,
        visible_has_ricia,
        visible_has_nature_collective,
    )

    # 次に「HTML内の追加ヒント」
    meta_hints = _extract_meta_hints(soup)
    ld_json = _extract_ld_json(soup)
    script_snips = _extract_script_snippets(soup)
    html_window = _extract_html_keyword_window(html)

    logger.info(
        "ai_enricher.fetch: extracted_parts meta=%s ld_json=%s script=%s html_window=%s",
        len(meta_hints),
        len(ld_json),
        len(script_snips),
        len(html_window),
    )

    if meta_hints:
        logger.info("ai_enricher.fetch: meta_head=%s", meta_hints[0][:300])
    if ld_json:
        logger.info("ai_enricher.fetch: ld_json_head=%s", ld_json[0][:300])
    if script_snips:
        logger.info("ai_enricher.fetch: script_head=%s", script_snips[0][:300])
    if html_window:
        logger.info("ai_enricher.fetch: html_window_head=%s", html_window[0][:300])

    parts: list[str] = []
    parts.extend(meta_hints)
    parts.extend(ld_json)
    parts.extend(script_snips)
    parts.extend(html_window)

    if parts:
        text = "\n".join(parts) + "\n\n" + text

    # さらに、最後の保険：可視テキストに instructor が無いなら、URLだけでも拾う
    if "instagram.com" not in text:
        for u in _URL_RE.findall(html):
            if "instagram.com" in u:
                text = f"[html_instagram] {u[:200]}\n\n" + text
                break

    has_instructor_kw = ("インストラクター" in text) or ("講師" in text)
    has_ricia = "リシア" in text
    has_nature_collective = ("Nature collective" in text) or ("Nature Collective" in text)

    logger.info(
        "ai_enricher.fetch: requests_result text_len=%s has_instructor_kw=%s has_ricia=%s has_nature_collective=%s url_final=%s",
        len(text or ""),
        has_instructor_kw,
        has_ricia,
        has_nature_collective,
        str(res.url),
    )

    return text


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    return (text or "")[:max_chars]


def _mail_body_text(msg: GmailMessage) -> str:
    if msg.text_plain:
        return msg.text_plain
    if msg.text_html:
        return normalize_html_to_text(msg.text_html)
    return ""


def build_enrich_input(
    msg: GmailMessage,
    event: Event,
    source_text: str,
    max_mail_chars: int,
    max_source_chars: int,
) -> str:
    body_excerpt = _truncate(_mail_body_text(msg), max_mail_chars)
    src_excerpt = _truncate(source_text or "", max_source_chars)

    lines = [
        "以下の既存抽出結果を一次情報として尊重し、不足項目のみ補完してください。",
        "",
        "[existing_event]",
        f"provider: {event.provider}",
        f"title: {event.title}",
        f"date: {event.date.isoformat()}",
        f"location_name: {event.location_name or ''}",
        f"address: {event.address or ''}",
        f"instructor: {event.instructor or ''}",
        f"reservation_id: {event.reservation_id or ''}",
        f"source_url: {event.source_url or ''}",
        f"base_price: {event.base_price or ''}",
        f"option_menus: {event.option_menus or []}",
        f"ticket_types: {event.ticket_types or []}",
        "",
        "[mail_summary]",
        f"subject: {msg.subject or ''}",
        f"snippet: {msg.snippet or ''}",
        "body_excerpt:",
        body_excerpt,
        "",
        "[source_page_text]",
        src_excerpt,
        "",
        "[rules]",
        "- 推測で埋めない。根拠不足はnullを返す。",
        "- title/date/provider/event_uid/reservation_id/source_url は絶対に変更提案しない。",
        "- 既存値がある項目は原則上書きしない。空欄またはダミー値のみ補完対象。",
        "- evidence は短い断片のみ（長文禁止）。",
    ]
    return "\n".join(lines).strip()


def _get_response_text(response: Any) -> Optional[str]:
    text = getattr(response, "output_text", None)
    if text:
        return text
    output = getattr(response, "output", None) or []
    for item in output:
        content = getattr(item, "content", None) or []
        for part in content:
            part_text = getattr(part, "text", None)
            if part_text:
                return part_text
    return None


def _call_responses_json_schema(client: OpenAI, model: str, input_text: str) -> EnrichResult:
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "You extract missing metadata for yoga event records. "
                            "Return strict JSON only. If unsure, return null for that field."
                        ),
                    }
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": input_text}]},
        ],
        response_format={"type": "json_schema", "json_schema": _ENRICH_JSON_SCHEMA},
    )
    text = _get_response_text(response)
    if not text:
        raise ValueError("empty enrich response")
    data = json.loads(text)
    result = EnrichResult.model_validate(data)
    result.evidence = [_short_evidence(x) for x in result.evidence if str(x).strip()]
    return result


def _call_chat_completions_tools(client: OpenAI, model: str, input_text: str) -> EnrichResult:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You extract missing metadata for yoga event records. "
                    "Return strict JSON only. If unsure, return null for that field."
                ),
            },
            {"role": "user", "content": input_text},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "event_meta_enrich_result",
                    "description": "Fill missing event metadata fields and return strict JSON.",
                    "parameters": _TOOL_SCHEMA,
                },
            }
        ],
        tool_choice={"type": "function", "function": {"name": "event_meta_enrich_result"}},
    )
    message = response.choices[0].message
    tool_calls = getattr(message, "tool_calls", None) or []
    if not tool_calls:
        raise ValueError("missing tool_calls in chat completion response")
    args = tool_calls[0].function.arguments
    data = json.loads(args)
    result = EnrichResult.model_validate(data)
    result.evidence = [_short_evidence(x) for x in result.evidence if str(x).strip()]
    return result


def _call_chat_completions_json_object(client: OpenAI, model: str, input_text: str) -> EnrichResult:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You extract missing metadata for yoga event records. "
                    "Return JSON object only. If unsure, use null for that field."
                ),
            },
            {"role": "user", "content": input_text},
        ],
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("empty chat completion content")
    data = json.loads(content)
    result = EnrichResult.model_validate(data)
    result.evidence = [_short_evidence(x) for x in result.evidence if str(x).strip()]
    return result


def call_openai_enrich(config: Config, input_text: str) -> EnrichResult:
    client = OpenAI(api_key=config.openai_api_key)
    try:
        result = _call_responses_json_schema(client, config.ai_enricher_model, input_text)
        logger.info("ai_enricher: route=responses")
        return result
    except TypeError as exc:
        logger.info("ai_enricher: responses fallback: %s", exc)
        try:
            result = _call_chat_completions_tools(client, config.ai_enricher_model, input_text)
            logger.info("ai_enricher: route=tools")
            return result
        except TypeError as exc2:
            logger.info("ai_enricher: chat.tools fallback: %s", exc2)
            result = _call_chat_completions_json_object(client, config.ai_enricher_model, input_text)
            logger.info("ai_enricher: route=json_object")
            return result


def _parse_provider_list(value: str) -> set[str]:
    return {p.strip().lower() for p in (value or "").split(",") if p.strip()}


def _is_target_provider(config: Config, provider: str) -> bool:
    targets = _parse_provider_list(config.ai_enricher_target_providers or "")
    if not targets:
        return False
    return (provider or "").strip().lower() in targets


def _is_skip_source_provider(config: Config, provider: str) -> bool:
    targets = _parse_provider_list(config.ai_enricher_skip_source_providers or "")
    if not targets:
        return False
    return (provider or "").strip().lower() in targets


def _looks_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _MISSING_TEXT_VALUES
    if isinstance(value, list):
        return len(value) == 0
    return False


def _ensure_ai_cache_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_cache (
            url TEXT PRIMARY KEY,
            fetched_at TEXT NOT NULL,
            source_text TEXT,
            enrich_json TEXT
        )
        """
    )
    conn.commit()


def _read_cache(conn: sqlite3.Connection, url: str, ttl_hours: int) -> Optional[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute("SELECT url, fetched_at, source_text, enrich_json FROM ai_cache WHERE url = ?", (url,))
    row = cur.fetchone()
    if row is None:
        return None

    fetched_at = row["fetched_at"]
    try:
        fetched_dt = datetime.fromisoformat(fetched_at)
    except Exception:
        return None

    if fetched_dt.tzinfo is None:
        fetched_dt = fetched_dt.replace(tzinfo=timezone.utc)

    # ttl_hours=0 は「常に期限切れ」
    if ttl_hours <= 0:
        return None

    age = datetime.now(timezone.utc) - fetched_dt
    if age > timedelta(hours=max(1, ttl_hours)):
        return None
    return row


def _upsert_cache(conn: sqlite3.Connection, url: str, source_text: Optional[str], enrich_json: Optional[str]) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ai_cache (url, fetched_at, source_text, enrich_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            fetched_at = excluded.fetched_at,
            source_text = COALESCE(excluded.source_text, ai_cache.source_text),
            enrich_json = COALESCE(excluded.enrich_json, ai_cache.enrich_json)
        """,
        (url, datetime.now(timezone.utc).isoformat(), source_text, enrich_json),
    )
    conn.commit()


def _get_cached_or_fetch_source_text(config: Config, url: str) -> str:
    conn = sqlite3.connect(config.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_ai_cache_table(conn)
        row = _read_cache(conn, url, config.ai_enricher_cache_ttl_hours)
        if row and row["source_text"]:
            return str(row["source_text"])

        source_text = fetch_source_text(url, config.ai_enricher_http_timeout_sec)
        _upsert_cache(conn, url, source_text=source_text, enrich_json=None)
        return source_text
    finally:
        conn.close()


def _get_cached_enrich(config: Config, url: str) -> Optional[EnrichResult]:
    conn = sqlite3.connect(config.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_ai_cache_table(conn)
        row = _read_cache(conn, url, config.ai_enricher_cache_ttl_hours)
        if not row or not row["enrich_json"]:
            return None
        data = json.loads(str(row["enrich_json"]))
        return EnrichResult.model_validate(data)
    except Exception:
        return None
    finally:
        conn.close()


def _cache_enrich(config: Config, url: str, enrich: EnrichResult) -> None:
    conn = sqlite3.connect(config.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_ai_cache_table(conn)
        _upsert_cache(conn, url, source_text=None, enrich_json=enrich.model_dump_json(ensure_ascii=False))
    finally:
        conn.close()


def _fill_if_missing(event: Event, field_name: str, value: Any) -> bool:
    if value is None:
        return False

    current = getattr(event, field_name)
    if not _looks_missing(current):
        return False

    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return False
        setattr(event, field_name, cleaned)
        return True

    if isinstance(value, list):
        cleaned_list = [str(x).strip() for x in value if str(x).strip()]
        if not cleaned_list:
            return False
        setattr(event, field_name, cleaned_list)
        return True

    setattr(event, field_name, value)
    return True


def enrich_event(config: Config, msg: GmailMessage, event: Event) -> tuple[Event, list[str], Optional[EnrichResult]]:
    if not config.yoga_classifier_enabled:
        return event, [], None
    if not config.ai_enricher_enabled:
        return event, [], None
    if not _is_target_provider(config, event.provider):
        return event, [], None

    fill_targets = ["instructor", "location_name", "address", "base_price", "option_menus", "ticket_types"]
    if not any(_looks_missing(getattr(event, key)) for key in fill_targets):
        return event, [], None

    provider = (event.provider or "").strip().lower()
    source_url = (event.source_url or "").strip()
    skip_source = _is_skip_source_provider(config, provider)

    if not skip_source and not source_url:
        return event, [], None

    try:
        if skip_source:
            logger.info("ai_enricher: skip_source provider=%s", provider)
            source_text = ""

            input_text = build_enrich_input(
                msg,
                event,
                source_text,
                max_mail_chars=config.ai_enricher_max_chars_mail,
                max_source_chars=config.ai_enricher_max_chars_source,
            )
            enrich = call_openai_enrich(config, input_text)
        else:
            cached = _get_cached_enrich(config, source_url)
            if cached:
                enrich = cached
            else:
                source_text = _get_cached_or_fetch_source_text(config, source_url)
                has_instructor_kw = ("インストラクター" in source_text) or ("講師" in source_text)
                logger.info(
                    "ai_enricher.debug: source_text_len=%s has_instructor_kw=%s",
                    len(source_text or ""),
                    has_instructor_kw,
                )

                input_text = build_enrich_input(
                    msg,
                    event,
                    source_text,
                    max_mail_chars=config.ai_enricher_max_chars_mail,
                    max_source_chars=config.ai_enricher_max_chars_source,
                )
                enrich = call_openai_enrich(config, input_text)
                _cache_enrich(config, source_url, enrich)

    except Exception:
        if config.ai_enricher_fail_open:
            logger.exception(
                "ai_enricher: failed but fail_open enabled provider=%s source_url=%s",
                event.provider,
                source_url,
            )
            logger.info("ai_enricher: fallback to original event due to fail_open=true")
            return event, [], None
        raise

    merged = event.model_copy(deep=True)
    filled: list[str] = []
    for key in fill_targets:
        if _fill_if_missing(merged, key, getattr(enrich, key)):
            filled.append(key)

    logger.info(
        "ai_enricher.diff: provider=%s skip_source=%s filled=%s before=%s ai=%s after=%s confidence=%s evidence=%s",
        provider,
        skip_source,
        filled,
        {
            "instructor": event.instructor,
            "location_name": event.location_name,
            "address": event.address,
            "base_price": event.base_price,
            "option_menus": event.option_menus,
            "ticket_types": event.ticket_types,
        },
        {
            "instructor": enrich.instructor if enrich else None,
            "location_name": enrich.location_name if enrich else None,
            "address": enrich.address if enrich else None,
            "base_price": enrich.base_price if enrich else None,
            "option_menus": enrich.option_menus if enrich else None,
            "ticket_types": enrich.ticket_types if enrich else None,
        },
        {
            "instructor": merged.instructor,
            "location_name": merged.location_name,
            "address": merged.address,
            "base_price": merged.base_price,
            "option_menus": merged.option_menus,
            "ticket_types": merged.ticket_types,
        },
        enrich.confidence if enrich else None,
        enrich.evidence if enrich else None,
    )

    return merged, filled, enrich
