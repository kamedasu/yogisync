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

    # 1) "https://mosh.jp/<digits>/home" の次の非空行
    for i, line in enumerate(lines):
        if "mosh.jp/" in line and "/home" in line:
            cand = _first_non_empty_line(lines, i + 1)
            if cand and not cand.startswith("http"):
                return cand.strip()

    # 2) ラベル系のフォールバック（将来のテンプレ変更対策）
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
            # 次の非空行を拾う
            nxt = _first_non_empty_line(lines, i + 1)
            if not nxt:
                continue

            # "<title> <url>" or "<title>https://..."
            m = re.search(r"^(?P<title>.+?)\s+(?P<url>https?://\S+)$", nxt)
            if m:
                return m.group("title").strip(), m.group("url").strip()

            # URLだけ別行の可能性もある
            title = nxt.strip()
            url = None
            for j in range(i + 2, min(i + 8, len(lines))):
                if lines[j].startswith("http"):
                    url = lines[j].strip()
                    break
            return title or None, url

    # フォールバック（既存実装互換）
    title = first_non_empty(
        extract_label_value(text, "サービス"),
        extract_label_value(text, "メニュー"),
        extract_label_value(text, "お申し込みのサービス"),
    )
    return title, None


def _extract_reservation_id_from_url(url: Optional[str]) -> Optional[str]:
    """
    例: https://mosh.jp/user/reservations/1068436
    """
    if not url:
        return None
    m = re.search(r"/reservations/(\d+)", url)
    if m:
        return m.group(1)
    return None


def _extract_source_url(text: str) -> Optional[str]:
    """
    MOSHメールには複数URLが出るので優先順位をつける：
      1) 申し込んだサービス（/services/<id>）
      2) 予約詳細（/user/reservations/<id>）
      3) 一般の extract_url
    """
    # 1) /services/<id>
    m = re.search(r"(https?://\S*mosh\.jp/services/\d+\S*)", text)
    if m:
        return m.group(1).strip()

    # 2) /user/reservations/<id>
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
            # 次行以降、区切り語が出るまで拾う
            for j in range(i + 1, min(i + 20, len(lines))):
                s = lines[j]
                if not s:
                    continue
                if any(s.startswith(x) for x in ["追加料金", "特別割引", "お申し込み人数", "お支払", "クーポン"]):
                    break
                # URLは除外（オプションの説明と混ざることがある）
                if s.startswith("http"):
                    continue
                opts.append(s)
            break
    return opts


def _extract_address(text: str) -> Optional[str]:
    """
    例:
      住所：藤沢市鵠沼松が岡１丁目６−３１
      住所: ...
    """
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
    """
    MOSH は「古民家名称：松の杜くげぬま」みたいな “場所名” が本文にある場合がある。
    無ければ None（＝住所のみ / locationは空でもOK）
    """
    name = first_non_empty(
        extract_label_value(text, "古民家名称"),
        extract_label_value(text, "会場"),
        extract_label_value(text, "場所"),
    )
    if name:
        return re.sub(r"\s+", " ", name).strip()
    return None


def parse_mosh(msg: GmailMessage) -> Optional[Event]:
    # MOSHは text_plain が安定（Gmail側で plain_len が取れている想定）
    text = msg.text_plain or msg.text_html or ""
    if not text:
        return None

    date = parse_first_datetime(text)
    if not date:
        return None

    instructor = _extract_instructor(text)

    title, reservation_url = _extract_title_and_reservation_url(text)
    if not title:
        # 件名の括弧内（…（イベント名））が来るケースにも備える
        title = msg.subject

    reservation_id = _extract_reservation_id_from_url(reservation_url)

    source_url = _extract_source_url(text)

    address = _extract_address(text)
    location_name = _extract_location_name(text)

    # 料金/オプション（※今はEventの格納先が無いので、confidence補助に使うだけ）
    base_price = _extract_base_price(text)
    options = _extract_option_lines(text)

    confidence = 0.8
    if title and instructor:
        confidence = 0.9
    if reservation_id:
        confidence = 1.0

    # base_price/options が取れていれば少しだけ上げる（=解析が正しい可能性UP）
    if base_price or options:
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
        confidence=confidence,
    )
