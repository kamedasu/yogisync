from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional, List, Tuple

from bs4 import BeautifulSoup

from ..models import Event, GmailMessage
from . import extract_label_value, parse_first_datetime, first_non_empty

logger = logging.getLogger(__name__)


# ----------------------------
# Patterns (Coubic / YES TOKYO)
# ----------------------------

# 予約番号
_RESERVATION_ID_RE = re.compile(r"(?:予約番号|予約ID)\s*[:：]\s*([0-9]{6,})")
_RESERVATION_ID_BULLET_RE = re.compile(r"^[◆■●]\s*予約番号\s*[:：]\s*([0-9]{6,})\s*$")

# 「◆予約日時:」は値が次行に折り返されるケースあり
_RESERVE_DT_BULLET_RE = re.compile(r"^[◆■●]\s*予約日時\s*[:：]\s*(.*)\s*$")
_RESERVE_DT_INLINE_RE = re.compile(r"予約日時\s*[:：]\s*(.+)$")

# YES TOKYO のサービスページ（欲しい source_url）
_SERVICE_PAGE_URL_RE = re.compile(r"https?://coubic\.com/yesstudio/\d+", re.I)

# 予約詳細URL（fallback）
_RV_URL_RE = re.compile(r"https?://coubic\.com/rv/[^\s\"'<>()]+", re.I)

# サービス名（タイトル候補）
# ★NEW: ご予約サービス を追加（テンプレがここ）
_SERVICE_LINE_RE = re.compile(
    r"^[◆■●]\s*(?:ご予約サービス|サービス|メニュー|予約内容|クラス|プログラム)\s*[:：]\s*(.*)\s*$"
)

# 金額（例: 4,400 円 (支払い済み) / ¥4,400 / ￥4,400）
_PRICE_ANY_RE = re.compile(r"(?P<amt>\d[\d,]*)\s*円(?:\s*\((?P<status>[^)]+)\))?")
_PRICE_YEN_RE = re.compile(r"[¥￥]\s*(?P<amt>\d[\d,]*)\s*(?:円)?(?:\s*\((?P<status>[^)]+)\))?")

# datetime patterns
_DT_JP_RE = re.compile(
    r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日\s*(?:[（(].*?[)）])?\s*(?P<h>\d{1,2})\s*[:：]\s*(?P<mi>\d{2})"
)
_DT_JP_DATE_ONLY_RE = re.compile(
    r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日(?:\s*[（(].*?[)）])?"
)
_DT_SLASH_RE = re.compile(
    r"(?P<y>\d{4})\s*/\s*(?P<m>\d{1,2})\s*/\s*(?P<d>\d{1,2})\s*(?:[（(].*?[)）])?\s*(?P<h>\d{1,2})\s*[:：]\s*(?P<mi>\d{2})"
)
_DT_SLASH_DATE_ONLY_RE = re.compile(r"(?P<y>\d{4})\s*/\s*(?P<m>\d{1,2})\s*/\s*(?P<d>\d{1,2})")
_DT_DASH_RE = re.compile(r"(?P<y>\d{4})\s*-\s*(?P<m>\d{1,2})\s*-\s*(?P<d>\d{1,2})\s*(?P<h>\d{1,2})\s*[:：]\s*(?P<mi>\d{2})")
_DT_DASH_DATE_ONLY_RE = re.compile(r"(?P<y>\d{4})\s*-\s*(?P<m>\d{1,2})\s*-\s*(?P<d>\d{1,2})")

_SUBJECT_POSITIVE_HINTS = ["予約が確定しました", "ご予約前日のご案内", "予約前日のご案内"]
_SUBJECT_NEGATIVE_HINTS = ["ご予約はいかがでしたか", "レビュー", "完全キャッシュレス"]


# ----------------------------
# Helpers
# ----------------------------

def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    return soup.get_text("\n")


def _normalize_lines(text: str) -> List[str]:
    lines: List[str] = []
    for ln in (text or "").splitlines():
        s = re.sub(r"\s+", " ", ln).strip()
        if s:
            lines.append(s)
    return lines


def _debug_excerpt(s: Optional[str], max_len: int = 240) -> str:
    if not s:
        return ""
    t = re.sub(r"\s+", " ", s).strip()
    return t if len(t) <= max_len else t[:max_len] + "..."


def _looks_like_target_email(msg: GmailMessage) -> bool:
    subj = (msg.subject or "").strip()
    if any(x in subj for x in _SUBJECT_NEGATIVE_HINTS):
        return False
    if any(x in subj for x in _SUBJECT_POSITIVE_HINTS):
        return True
    hay = (msg.text_plain or "") + "\n" + (msg.text_html or "")
    return ("予約番号" in hay and "予約日時" in hay)


def _extract_reservation_id(lines: List[str], raw_text: str) -> Optional[str]:
    for ln in lines:
        m = _RESERVATION_ID_BULLET_RE.match(ln)
        if m:
            return m.group(1)

    m = _RESERVATION_ID_RE.search(raw_text)
    if m:
        return m.group(1)

    v = first_non_empty(
        extract_label_value(raw_text, "予約番号"),
        extract_label_value(raw_text, "予約ID"),
    )
    if v:
        vv = re.sub(r"\D+", "", v)
        return vv or v.strip()
    return None


def _parse_datetime_any(s: str) -> Tuple[Optional[datetime], bool]:
    """
    return: (dt, time_unknown)
    """
    s = (s or "").strip()
    if not s:
        return None, False

    s2 = s.replace("〜", " ").replace("～", " ").replace("–", " ").replace("—", " ")

    # time included
    for pat in (_DT_JP_RE, _DT_SLASH_RE, _DT_DASH_RE):
        m = pat.search(s2)
        if m:
            y = int(m.group("y"))
            mo = int(m.group("m"))
            d = int(m.group("d"))
            h = int(m.group("h"))
            mi = int(m.group("mi"))
            return datetime(y, mo, d, h, mi), False

    # date only
    for pat in (_DT_JP_DATE_ONLY_RE, _DT_SLASH_DATE_ONLY_RE, _DT_DASH_DATE_ONLY_RE):
        m = pat.search(s2)
        if m:
            y = int(m.group("y"))
            mo = int(m.group("m"))
            d = int(m.group("d"))
            return datetime(y, mo, d, 12, 0), True

    return None, False


def _take_next_value_lines(lines: List[str], start_idx: int, max_lines: int = 2) -> str:
    parts: List[str] = []
    for j in range(start_idx + 1, min(len(lines), start_idx + 1 + 12)):
        if not lines[j]:
            continue
        parts.append(lines[j])
        if len(parts) >= max_lines:
            break
    return " ".join(parts).strip()


def _extract_datetime(lines: List[str], full_text: str) -> Tuple[Optional[datetime], bool, Optional[str], Optional[str]]:
    """
    return: (dt, time_unknown, matched_line, candidate_value)
    """
    for i, ln in enumerate(lines):
        m = _RESERVE_DT_BULLET_RE.match(ln)
        if not m:
            continue
        value = (m.group(1) or "").strip()
        if not value:
            value = _take_next_value_lines(lines, i, max_lines=2)
        dt, unk = _parse_datetime_any(value)
        if dt:
            return dt, unk, ln, value
        return None, False, ln, value

    for ln in lines:
        if "予約日時" not in ln:
            continue
        m = _RESERVE_DT_INLINE_RE.search(ln)
        val = (m.group(1) if m else ln).strip()
        dt, unk = _parse_datetime_any(val)
        if dt:
            return dt, unk, ln, val

    dt2 = parse_first_datetime(full_text)
    if dt2:
        return dt2, False, None, None

    return None, False, None, None


def _extract_title(lines: List[str], msg: GmailMessage) -> Optional[str]:
    """
    テンプレ仕様:
      ◆ご予約サービス:
        FRIDAY
        https://coubic.com/yesstudio/3966189

    なので「ご予約サービス」を最優先で拾う（次行に折り返しも吸収）。
    """
    # 1) ラベル行（◆ご予約サービス: ...）を最優先
    for i, ln in enumerate(lines):
        m = _SERVICE_LINE_RE.match(ln)
        if not m:
            continue
        value = (m.group(1) or "").strip()
        if not value:
            value = _take_next_value_lines(lines, i, max_lines=1)

        value = value.strip()
        if value:
            value = re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()
            return value or None

    # 2) さらに保険：前行が「ご予約サービス」っぽくて、当行が英字の1語（FRIDAY等）
    for i in range(1, len(lines)):
        prev = lines[i - 1]
        cur = lines[i]
        if "ご予約サービス" in prev and re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{1,40}", cur):
            return cur.strip()

    # 3) フォールバック：subject整形（弱い）
    subj = (msg.subject or "").strip()
    subj = subj.replace("[YES TOKYO STUDIO]", "").strip()
    subj = re.sub(r"\s*様の予約が確定しました\s*$", "", subj).strip()
    return subj or None


def _extract_service_page_url(text: str) -> Optional[str]:
    m = _SERVICE_PAGE_URL_RE.search(text or "")
    if m:
        return m.group(0).rstrip(".,);]")
    return None


def _extract_rv_url(text: str) -> Optional[str]:
    m = _RV_URL_RE.search(text or "")
    if m:
        return m.group(0).rstrip(".,);]")
    return None


def _extract_source_url(full_text: str) -> Optional[str]:
    service_url = _extract_service_page_url(full_text)
    if service_url:
        return service_url
    return _extract_rv_url(full_text)


def _extract_price(lines: List[str], full_text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    return: (base_price, payment_status)
      base_price: "¥4,400"
      payment_status: "支払い済み" etc
    """
    for ln in lines:
        if "円" not in ln and "¥" not in ln and "￥" not in ln:
            continue

        m = _PRICE_YEN_RE.search(ln)
        if m:
            amt = m.group("amt")
            status = m.group("status")
            return f"¥{amt}", status.strip() if status else None

        m2 = _PRICE_ANY_RE.search(ln)
        if m2:
            amt = m2.group("amt")
            status = m2.group("status")
            return f"¥{amt}", status.strip() if status else None

    m = _PRICE_YEN_RE.search(full_text or "")
    if m:
        amt = m.group("amt")
        status = m.group("status")
        return f"¥{amt}", status.strip() if status else None

    m2 = _PRICE_ANY_RE.search(full_text or "")
    if m2:
        amt = m2.group("amt")
        status = m2.group("status")
        return f"¥{amt}", status.strip() if status else None

    return None, None


# ----------------------------
# Main parser
# ----------------------------

def parse_yes_tokyo(msg: GmailMessage) -> Optional[Event]:
    raw = msg.text_plain or msg.text_html or ""
    if not raw:
        return None

    if not _looks_like_target_email(msg):
        logger.info(
            "yes_tokyo.parse debug: not target email msg_id=%s subject=%s from=%s snippet=%s",
            msg.id,
            msg.subject or "",
            msg.from_email or "",
            _debug_excerpt(msg.snippet or "", 160),
        )
        return None

    if msg.text_html and not msg.text_plain:
        text = _html_to_text(msg.text_html)
    else:
        text = msg.text_plain or msg.text_html or ""

    lines = _normalize_lines(text)
    full_text = text

    reservation_id = _extract_reservation_id(lines, full_text)
    dt, time_unknown, matched_dt_line, dt_candidate_value = _extract_datetime(lines, full_text)

    title = _extract_title(lines, msg)
    base_price, payment_status = _extract_price(lines, full_text)
    source_url = _extract_source_url(full_text)

    dt_like_lines = [ln for ln in lines if "予約日時" in ln][:5]
    title_like_lines = [ln for ln in lines if ("サービス" in ln or "メニュー" in ln or "予約内容" in ln or "ご予約サービス" in ln)][:8]

    logger.info(
        "yes_tokyo.parse debug: extracted msg_id=%s subject=%s reservation_id=%s dt=%s time_unknown=%s matched_dt_line=%s dt_candidate_value=%s dt_like_lines=%s title=%s title_like_lines=%s base_price=%s payment_status=%s source_url=%s",
        msg.id,
        msg.subject or "",
        reservation_id,
        dt.isoformat() if dt else None,
        time_unknown,
        matched_dt_line,
        dt_candidate_value,
        dt_like_lines,
        title,
        title_like_lines,
        base_price,
        payment_status,
        source_url,
    )

    if not dt:
        return None

    option_menus: Optional[list[str]] = None
    if payment_status:
        option_menus = [f"payment_status: {payment_status}"]

    return Event(
        provider="yes_tokyo",
        title=title or "YES TOKYO Reservation",
        date=dt,
        location_name="YES TOKYO STUDIO",
        address=None,
        instructor=None,
        reservation_id=reservation_id,
        source_url=source_url,
        base_price=base_price,
        option_menus=option_menus,
        confidence=1.0 if reservation_id and source_url else 0.9,
        time_unknown=time_unknown,
    )
