from __future__ import annotations

import json
import re
from html import unescape
from typing import Any, Dict, Optional, Tuple, List

from bs4 import BeautifulSoup

from ..models import Event, GmailMessage
from . import extract_label_value, parse_first_datetime, first_non_empty


# ----------------------------
# JSON-LD helpers
# ----------------------------

def _extract_jsonld(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Peatixのメールは application/ld+json に EventReservation が入っていることが多い。
    例:
      {
        "@type": "EventReservation",
        "reservationNumber": "34041688",
        "reservationFor": { ... Event ... }
      }

    戻り値:
      - reservationNumber (str)
      - reservationFor (dict) : Event情報
    """
    for script in soup.find_all("script"):
        t = (script.get("type") or "").strip().lower()
        if t != "application/ld+json":
            continue

        raw = script.string or script.get_text() or ""
        raw = unescape(raw).strip()
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        if isinstance(data, dict):
            rid = data.get("reservationNumber")
            event_info = data.get("reservationFor")
            rid_s = str(rid).strip() if rid is not None else None
            event_dict = event_info if isinstance(event_info, dict) else None
            if rid_s or event_dict:
                return (rid_s or None, event_dict)

    return (None, None)


def _jsonld_event_url(event_info: Optional[Dict[str, Any]]) -> Optional[str]:
    if not event_info:
        return None
    url = event_info.get("url")
    if url:
        s = str(url).strip()
        return s or None
    return None


def _jsonld_venue(event_info: Optional[Dict[str, Any]]) -> Optional[str]:
    if not event_info:
        return None
    loc = event_info.get("location")
    if isinstance(loc, dict):
        name = loc.get("name")
        if name:
            s = str(name).strip()
            return s or None
    return None


def _jsonld_address(event_info: Optional[Dict[str, Any]]) -> Optional[str]:
    if not event_info:
        return None

    loc = event_info.get("location")
    if not isinstance(loc, dict):
        return None

    addr = loc.get("address")
    if not isinstance(addr, dict):
        return None

    parts: List[str] = []

    postal = addr.get("postalCode")
    region = addr.get("addressRegion")
    locality = addr.get("addressLocality")
    street = addr.get("streetAddress")

    if postal:
        parts.append(f"〒{str(postal).strip()}")
    if region:
        parts.append(str(region).strip())
    if locality:
        loc_s = str(locality).strip()
        if not parts or parts[-1] != loc_s:
            parts.append(loc_s)
    if street:
        parts.append(str(street).strip())

    s = " ".join([p for p in parts if p])
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# ----------------------------
# Text/HTML helpers
# ----------------------------

def _extract_line_after(text: str, marker: str, lookahead: int = 8) -> Optional[str]:
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        if line == marker:
            for j in range(i + 1, min(i + 1 + lookahead, len(lines))):
                if lines[j]:
                    return lines[j]
    return None


def _cleanup_peatix_title(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = re.sub(r"\s+", " ", s).strip()

    s = re.sub(r"^\s*【\s*Peatix\s*】\s*", "", s).strip()
    s = re.sub(r"^\s*\[\s*Peatix\s*\]\s*", "", s).strip()

    s = re.sub(r"\s*のチケット(お申し込み)?詳細\s*$", "", s).strip()
    s = re.sub(r"\s*[（(].+?[)）]\s*$", "", s).strip()

    return s or None


def _extract_title_from_html(soup: BeautifulSoup) -> Optional[str]:
    # <h1> が「Peatixアプリがチケットです!」になりがちなので <h2>優先
    h2 = soup.find("h2")
    if h2:
        return _cleanup_peatix_title(h2.get_text(strip=True))
    return None


def _extract_title_from_body_text(text: str) -> Optional[str]:
    raw = first_non_empty(
        _extract_line_after(text, "受信トレイ"),
        _extract_line_after(text, "予定のタイトル"),
        extract_label_value(text, "予定のタイトル"),
    )
    return _cleanup_peatix_title(raw)


def _extract_reservation_id(text: str, soup: BeautifulSoup) -> Optional[str]:
    rid_jsonld, _ = _extract_jsonld(soup)
    if rid_jsonld:
        digits = re.sub(r"\D+", "", rid_jsonld)
        return digits or rid_jsonld

    m = re.search(r"(確認番号|予約番号)\s*[:：]?\s*([0-9]{5,})", text)
    if m:
        return m.group(2)

    rid = first_non_empty(
        _extract_line_after(text, "確認番号"),
        _extract_line_after(text, "予約番号"),
        extract_label_value(text, "確認番号"),
        extract_label_value(text, "予約番号"),
    )
    if not rid:
        return None

    digits = re.sub(r"\D+", "", rid.strip())
    return digits or rid.strip()


def _extract_address(text: str, soup: BeautifulSoup) -> Optional[str]:
    _, event_info = _extract_jsonld(soup)
    addr_jsonld = _jsonld_address(event_info)
    if addr_jsonld:
        return addr_jsonld

    addr = first_non_empty(
        extract_label_value(text, "住所"),
        extract_label_value(text, "所在地"),
    )
    if addr:
        return re.sub(r"\s+", " ", addr).strip()

    m = re.search(r"住所\s*[:：]?\s*(.+)", text)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()

    return None


# ----------------------------
# URL extraction (final)
# ----------------------------

# 除外したい “汎用/案内/配信用” ドメインやパス
_EXCLUDED_PREFIXES = (
    "https://help.peatix.com/",
    "http://help.peatix.com/",
    "https://about.peatix.com/",
    "http://about.peatix.com/",
    "https://cdn.peatix.com/",
    "http://cdn.peatix.com/",
    "https://t.peatix.com/",
    "http://t.peatix.com/",
)

_EXCLUDED_PATH_KEYWORDS = (
    "/pricing",
    "pricing.html",
    "/about",
    "/help",
    "/customer",
    "/portal",
)

# 期待値：イベント用サブドメイン
_EVENT_SUBDOMAIN_RE = re.compile(r"^https?://(?!about\.|help\.|cdn\.|t\.)[a-z0-9][a-z0-9-]*\.peatix\.com/?$", re.I)


def _normalize_url(url: str) -> str:
    u = (url or "").strip()
    u = u.replace("&amp;", "&")
    return u


def _is_excluded_url(url: str) -> bool:
    u = _normalize_url(url)
    if not u:
        return True
    for p in _EXCLUDED_PREFIXES:
        if u.startswith(p):
            return True
    # peatix.com の pricing/about/help なども除外
    for kw in _EXCLUDED_PATH_KEYWORDS:
        if kw in u:
            return True
    return False


def _score_peatix_url(url: str) -> int:
    """
    “イベントページっぽさ”でスコアリング。
    高いほど優先。
    """
    u = _normalize_url(url)
    if _is_excluded_url(u):
        return -10_000

    score = 0

    # 1) 期待値: https://<event>.peatix.com/
    if _EVENT_SUBDOMAIN_RE.match(u):
        score += 10_000

    # 2) peatix.com/event/<id>
    if "peatix.com/event/" in u:
        score += 9_000

    # 3) *.peatix.com/view/<id>
    if re.search(r"^https?://[^/]+\.peatix\.com/view/", u):
        score += 8_500

    # 4) その他 peatix.com
    if "peatix.com" in u:
        score += 100

    if u.startswith("https://"):
        score += 5

    # URLが短すぎる(=汎用)のを少し減点
    if len(u) < 25:
        score -= 50

    return score


def _extract_urls_from_raw(raw: str) -> List[str]:
    """
    aタグに無い直書きURLも拾う（.emlに直書きされてるケース対応）
    """
    if not raw:
        return []
    urls = re.findall(r"https?://[^\s\"'<>()]+", raw)
    # 末尾の記号を軽く除去
    cleaned: List[str] = []
    for u in urls:
        u2 = u.rstrip(".,);]")
        cleaned.append(u2)
    return cleaned


def _extract_peatix_url(soup: BeautifulSoup, raw_html: str, text: str) -> Optional[str]:
    """
    source_url は「イベントページURL」を入れる。

    優先順位:
      - HTML/テキスト内の直書き含め候補を全部集める
      - “イベント用サブドメイン” を最優先で採用（spinetwistcurryyoga0211.peatix.com など）
      - それが無い場合に peatix.com/event/ を採用
      - t.peatix / cdn / about / help / pricing は除外
    """
    candidates: List[str] = []

    # JSON-LD
    _, event_info = _extract_jsonld(soup)
    url_jsonld = _jsonld_event_url(event_info)
    if url_jsonld:
        candidates.append(url_jsonld)

    # anchors
    for a in soup.find_all("a"):
        href = _normalize_url(a.get("href") or "")
        if href:
            candidates.append(href)

    # raw html / text から直書きURLも拾う
    candidates.extend(_extract_urls_from_raw(raw_html))
    candidates.extend(_extract_urls_from_raw(text))

    # peatix関連だけに寄せる（ノイズ削減）
    candidates = [c for c in candidates if "peatix.com" in c]

    best_url: Optional[str] = None
    best_score = -10_000

    for u in candidates:
        sc = _score_peatix_url(u)
        if sc > best_score:
            best_score = sc
            best_url = _normalize_url(u)

    return best_url if best_url and best_score > 0 else None


# ----------------------------
# Main parser
# ----------------------------

def parse_peatix(msg: GmailMessage) -> Optional[Event]:
    """
    env例:
      gmail_query==from:tickets@peatix.com subject:"チケットお申し込み詳細"

    取りたいもの:
      - title: イベント名
      - reservation_id: 確認番号
      - address: 住所
      - source_url: イベントページURL（例: https://xxxx.peatix.com/）
    """
    html = msg.text_html or ""
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n")

    title = first_non_empty(
        _extract_title_from_html(soup),
        _extract_title_from_body_text(text),
        _cleanup_peatix_title(msg.subject),
    )

    date = parse_first_datetime(text)
    if not date:
        return None

    _, event_info = _extract_jsonld(soup)

    venue = first_non_empty(
        _jsonld_venue(event_info),
        extract_label_value(text, "会場"),
        extract_label_value(text, "場所"),
    )

    address = _extract_address(text, soup)
    reservation_id = _extract_reservation_id(text, soup)

    source_url = _extract_peatix_url(soup, raw_html=html, text=text)

    return Event(
        provider="peatix",
        title=title or "Peatix Event",
        date=date,
        location_name=venue,
        address=address,
        instructor=None,
        reservation_id=reservation_id,
        source_url=source_url,
        confidence=1.0 if title and reservation_id else 0.9,
    )
