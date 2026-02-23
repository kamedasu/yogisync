from __future__ import annotations

import re
from typing import Optional, List, Tuple

from bs4 import BeautifulSoup

from ..models import Event, GmailMessage
from . import extract_label_value, parse_first_datetime, first_non_empty


_URL_RE = re.compile(r"https?://[^\s\"'<>()]+")
_YEN_RE = re.compile(r"[¥￥]\s?\d[\d,]*")
_RESERVATION_RE = re.compile(r"(?:予約番号|予約ID)\s*[:：#]?\s*([A-Za-z0-9-]+)")


def _to_text_and_soup(msg: GmailMessage) -> Tuple[str, Optional[BeautifulSoup]]:
    html = msg.text_html or ""
    if html:
        soup = BeautifulSoup(html, "lxml")
        return soup.get_text("\n"), soup

    text = msg.text_plain or ""
    return text, None


def _normalize_url(u: str) -> str:
    return (u or "").replace("&amp;", "&").strip().rstrip(".,);]")


def _extract_urls(text: str) -> List[str]:
    return [_normalize_url(x) for x in _URL_RE.findall(text or "")]


def _extract_block_value(text: str, label: str, max_lookahead: int = 5) -> Optional[str]:
    """
    例:
    ◆予約番号:
      87246923

    ◆金額(税込み):
      4,400 円 (支払い済み)
    """
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue

        # 完全一致寄り / コロン有無ゆらぎ対応
        if line.startswith(label):
            vals: List[str] = []
            for j in range(i + 1, min(i + 1 + max_lookahead, len(lines))):
                s = lines[j].strip()
                if not s:
                    if vals:
                        break
                    continue
                if s.startswith("◆"):
                    break
                vals.append(s)

            if vals:
                return " ".join(vals).strip()
    return None


def _extract_reservation_id(text: str) -> Optional[str]:
    v = _extract_block_value(text, "◆予約番号") or _extract_block_value(text, "予約番号")
    if v:
        m = re.search(r"([A-Za-z0-9-]+)", v)
        if m:
            return m.group(1)
        return v.strip()

    m = _RESERVATION_RE.search(text or "")
    if m:
        return m.group(1).strip()

    return first_non_empty(
        extract_label_value(text, "予約番号"),
        extract_label_value(text, "予約ID"),
    )


def _extract_yes_date(text: str):
    # 既存ヘルパー優先でOK（2025年12月05日 (金) 19:00 ~ 20:00 を拾える想定）
    return parse_first_datetime(text)


def _extract_title(text: str) -> Optional[str]:
    """
    添付テンプレでは「◆ご予約サービス:」の次行に FRIDAY が入る。
    """
    v = first_non_empty(
        _extract_block_value(text, "◆ご予約サービス"),
        _extract_block_value(text, "ご予約サービス"),
        _extract_block_value(text, "◆クラス"),
        _extract_block_value(text, "◆プログラム"),
        extract_label_value(text, "クラス"),
        extract_label_value(text, "プログラム"),
        extract_label_value(text, "レッスン"),
    )
    if not v:
        return None

    # URLだけの行はタイトルとして使わない
    if v.startswith("http://") or v.startswith("https://"):
        return None

    # "FRIDAY https://coubic.com/..." のように連結された場合にURL以降を切る
    v = re.sub(r"\s+https?://\S+$", "", v).strip()
    return v or None


def _extract_location_name(text: str) -> Optional[str]:
    return first_non_empty(
        _extract_block_value(text, "◆提供者"),
        _extract_block_value(text, "提供者"),
        _extract_block_value(text, "◆店舗"),
        _extract_block_value(text, "店舗"),
        "YES TOKYO STUDIO",
    )


def _extract_price_option(text: str) -> Optional[list[str]]:
    """
    要件: 金額(税込み)を option_menus に格納
    例: 4,400 円 (支払い済み) -> ¥4,400
    """
    v = first_non_empty(
        _extract_block_value(text, "◆金額(税込み)"),
        _extract_block_value(text, "金額(税込み)"),
        _extract_block_value(text, "◆金額（税込み）"),
        _extract_block_value(text, "金額（税込み）"),
    )

    target = v or text or ""
    m = _YEN_RE.search(target)
    if m:
        price = m.group(0).replace("￥", "¥").replace(" ", "")
        return [price]

    # "4,400 円" 形式だけで ¥ がない場合
    m2 = re.search(r"(\d[\d,]*)\s*円", target)
    if m2:
        return [f"¥{m2.group(1)}"]

    return None


def _extract_source_url(text: str, soup: Optional[BeautifulSoup]) -> Optional[str]:
    """
    要件: 「ご予約内容の詳細確認、キャンセル・変更はこちら」を source_url にしたい。
    実メールでは tracking link のアンカー表示テキストに https://coubic.com/rv/... が出るので、
    まず text側の coubic.com/rv/ を優先。
    """
    urls = _extract_urls(text)

    # 1) 最優先: coubic の予約詳細URL
    for u in urls:
        if "coubic.com/rv/" in u:
            return u

    # 2) 次点: yesstudio の予約サービスURL（一覧/サービス詳細）
    for u in urls:
        if "coubic.com/yesstudio/" in u:
            return u

    # 3) HTMLアンカーの文言ベース（tracking hrefしか無い場合のfallback）
    if soup:
        for a in soup.find_all("a"):
            label = a.get_text(" ", strip=True)
            href = _normalize_url(a.get("href") or "")
            if not href:
                continue
            if ("詳細確認" in label) or ("キャンセル" in label and "変更" in label):
                return href

    # 4) その他 coubic URL
    for u in urls:
        if "coubic.com" in u:
            return u

    return urls[0] if urls else None


def parse_yes_tokyo(msg: GmailMessage) -> Optional[Event]:
    text, soup = _to_text_and_soup(msg)
    if not text:
        return None

    date = _extract_yes_date(text)
    if not date:
        return None

    title = _extract_title(text) or first_non_empty(msg.subject, "YES TOKYO Reservation")
    reservation_id = _extract_reservation_id(text)
    location_name = _extract_location_name(text)
    source_url = _extract_source_url(text, soup)
    option_menus = _extract_price_option(text)

    return Event(
        provider="yes_tokyo",
        title=title or "YES TOKYO Class",
        date=date,
        location_name=location_name,
        address=None,
        instructor=None,
        reservation_id=reservation_id,
        source_url=source_url,
        option_menus=option_menus,
        confidence=1.0,
    )
