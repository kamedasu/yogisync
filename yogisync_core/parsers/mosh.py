from __future__ import annotations

import re
from typing import Optional, List

from ..models import Event, GmailMessage
from . import extract_label_value, extract_url, parse_first_datetime, first_non_empty


def _first_non_empty_line(lines: List[str], start: int = 0) -> Optional[str]:
    for i in range(start, len(lines)):
        s = (lines[i] or "").strip()
        if s:
            return s
    return None


def _extract_instructor(text: str) -> Optional[str]:
    """
    MOSHの予約メール（text/plain）では、冒頭に
      https://mosh.jp/<user_id>/home...
      <講師名>
    の形で出ることが多いので、まずそこを最優先で拾う。
    """
    lines = text.splitlines()

    for i, line in enumerate(lines):
        if "mosh.jp/" in line and "/home" in line:
            cand = _first_non_empty_line(lines, i + 1)
            if cand and not cand.startswith("http"):
                return cand.strip()

    return first_non_empty(
        extract_label_value(text, "講師"),
        extract_label_value(text, "講師名"),
        extract_label_value(text, "インストラクター"),
        extract_label_value(text, "クリエイター"),
    )


def _extract_title_and_reservation_url(text: str) -> tuple[Optional[str], Optional[str]]:
    """
    text/plain では以下のブロックが安定していることが多い：
      お申し込みのサービス
      <イベント名> https://mosh.jp/user/reservations/1234567
    """
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        if line == "お申し込みのサービス":
            nxt = _first_non_empty_line(lines, i + 1)
            if not nxt:
                continue

            m = re.search(r"^(?P<title>.+?)\s+(?P<url>https?://\S+)$", nxt)
            if m:
                return m.group("title").strip(), m.group("url").strip()

            title = nxt.strip()
            url = None
            for j in range(i + 2, min(i + 8, len(lines))):
                if lines[j].startswith("http"):
                    url = lines[j].strip()
                    break
            return title or None, url

    title = first_non_empty(
        extract_label_value(text, "サービス"),
        extract_label_value(text, "メニュー"),
        extract_label_value(text, "お申し込みのサービス"),
    )
    return title, None


def _extract_reservation_id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"/reservations/(\d+)", url)
    if m:
        return m.group(1)
    return None


def _extract_source_url(text: str) -> Optional[str]:
    m = re.search(r"(https?://\S*mosh\.jp/services/\d+\S*)", text)
    if m:
        return m.group(1).strip()

    m = re.search(r"(https?://\S*mosh\.jp/user/reservations/\d+\S*)", text)
    if m:
        return m.group(1).strip()

    return extract_url(text)


def _extract_base_price(text: str) -> Optional[str]:
    """
    例: 基本料金: ¥2,500
    """
    m = re.search(r"基本料金\s*[:：]\s*([¥￥]\s*[0-9,]+)", text)
    if m:
        return re.sub(r"\s+", "", m.group(1))
    return None


def _extract_option_lines(text: str) -> List[str]:
    """
    例:
      オプションメニュー:
      レンタルヨガマット×1(￥200)
    """
    lines = [l.strip() for l in text.splitlines()]
    opts: List[str] = []
    for i, line in enumerate(lines):
        if line.startswith("オプションメニュー"):
            for j in range(i + 1, min(i + 30, len(lines))):
                s = lines[j]
                if not s:
                    continue
                if any(s.startswith(x) for x in ["追加料金", "特別割引", "お申し込み人数", "お支払", "クーポン", "合計"]):
                    break
                if s.startswith("http"):
                    continue
                opts.append(s)
            break
    return opts


def _extract_address(text: str) -> Optional[str]:
    addr = first_non_empty(
        extract_label_value(text, "住所"),
        extract_label_value(text, "所在地"),
    )
    if addr:
        return re.sub(r"\s+", " ", addr).strip()

    m = re.search(r"住所\s*[:：]\s*(.+)", text)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()

    return None


def _extract_location_name(text: str) -> Optional[str]:
    name = first_non_empty(
        extract_label_value(text, "古民家名称"),
        extract_label_value(text, "会場"),
        extract_label_value(text, "場所"),
    )
    if name:
        return re.sub(r"\s+", " ", name).strip()
    return None


def parse_mosh(msg: GmailMessage) -> Optional[Event]:
    text = msg.text_plain or msg.text_html or ""
    if not text:
        return None

    date = parse_first_datetime(text)
    if not date:
        return None

    instructor = _extract_instructor(text)

    title, reservation_url = _extract_title_and_reservation_url(text)
    if not title:
        title = msg.subject

    reservation_id = _extract_reservation_id_from_url(reservation_url)
    source_url = _extract_source_url(text)

    address = _extract_address(text)
    location_name = _extract_location_name(text)

    base_price = _extract_base_price(text)
    option_menus = _extract_option_lines(text)
    if not option_menus:
        option_menus = None

    confidence = 0.8
    if title and instructor:
        confidence = 0.9
    if reservation_id:
        confidence = 1.0
    if base_price or option_menus:
        confidence = min(1.0, confidence + 0.05)

    return Event(
        provider="mosh",
        title=title or "MOSH Reservation",
        date=date,
        location_name=location_name,
        address=address,
        instructor=instructor,
        reservation_id=reservation_id,
        source_url=source_url,
        base_price=base_price,
        option_menus=option_menus,
        confidence=confidence,
    )
