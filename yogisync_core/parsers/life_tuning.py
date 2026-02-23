from __future__ import annotations

import quopri
import re
from datetime import datetime
from typing import Optional, List, Tuple

from bs4 import BeautifulSoup

from ..models import Event, GmailMessage
from . import extract_label_value, parse_first_datetime, first_non_empty


# ----------------------------
# URL / text patterns
# ----------------------------

_URL_RE = re.compile(r"https?://[^\s\"'<>()]+")
_YEN_RE = re.compile(r"[¥￥]\s?\d[\d,]*")
_ORDER_NO_RE = re.compile(r"#?(LTO\d{4,})", re.I)

# account の正規URL（最終的に source_url に入れたい）
_ACCOUNT_ORDER_RE = re.compile(
    r"https?://account\.life-tuning-online\.com/orders/([a-f0-9]{20,})(?:\?[^\s]*)?",
    re.I,
)

# Shopify 認証URL（メールテンプレ内にある元URL）
# quoted-printable で hash が改行されるため [a-f0-9=\r\n]+ を許容
_SHOPIFY_AUTH_HASH_RE = re.compile(
    r"https?://life-tuning-online\.com/\d+/orders/([a-f0-9=\r\n]+?)/authenticate\?",
    re.I,
)

# 地図URL（maps.app.goo.gl）
_MAP_URL_RE = re.compile(r"https?://maps\.app\.goo\.gl/[^\s\"'<>()]+", re.I)

# 日付行パターン（時間あり/なし）
# 例: 2/20（金）19:30
_DATE_LINE_MMDD_TIME_RE = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})\s*[（(][月火水木金土日][)）]\s*(\d{1,2}):(\d{2})\s*$"
)
# 例: 1月17日（土）
_DATE_LINE_JP_RE = re.compile(
    r"^\s*(\d{1,2})月(\d{1,2})日\s*[（(][月火水木金土日][)）]\s*$"
)

# 例: 2/20（金）19:30 ～ 温め、ゆるめるヨガ × 1（1行型）
_ONE_LINE_ITEM_RE = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})\s*[（(][月火水木金土日][)）]\s*(\d{1,2}):(\d{2})\s*[~〜～-]\s*(.+?)\s*[×xX]\s*(\d+)\s*$"
)

# 例: 1月17日（土）Inner Change, New Beginning（同じ行にタイトル前半あり）
_JP_DATE_WITH_TITLE_HEAD_RE = re.compile(
    r"^\s*(\d{1,2})月(\d{1,2})日\s*[（(][月火水木金土日][)）]\s*(.+?)\s*$"
)


# ----------------------------
# basic helpers
# ----------------------------

def _to_text_and_soup(msg: GmailMessage) -> Tuple[str, Optional[BeautifulSoup]]:
    html = msg.text_html or ""
    if html:
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n")
        return text, soup
    return (msg.text_plain or ""), None


def _normalize_url(u: str) -> str:
    return (u or "").replace("&amp;", "&").strip().rstrip(".,);]")


def _normalize_lines(text: str) -> List[str]:
    lines: List[str] = []
    for ln in (text or "").splitlines():
        s = re.sub(r"\s+", " ", ln).strip()
        if s:
            lines.append(s)
    return lines


def _guess_year(month: int, day: int, now: Optional[datetime] = None) -> int:
    """
    メール本文に年が無いことが多いので、現在日に最も近い年を推定する。
    """
    now = now or datetime.now()
    cands = [now.year - 1, now.year, now.year + 1]

    best_year = now.year
    best_diff = 10**9
    for y in cands:
        try:
            d = datetime(y, month, day)
        except ValueError:
            continue
        diff = abs((d.date() - now.date()).days)
        if diff < best_diff:
            best_diff = diff
            best_year = y
    return best_year


def _is_noise_title_line(s: str) -> bool:
    if not s:
        return True

    s2 = s.strip()

    # 数量のみ / 金額のみ
    if re.fullmatch(r"[×xX]?\s*\d+", s2):
        return True
    if re.fullmatch(r"[¥￥]?\s*\d[\d,]*(?:円)?", s2):
        return True

    ng_keywords = [
        "レッスン",
        "注文概要",
        "Order Details",
        "小計",
        "合計",
        "数量",
        "税込",
        "お問い合わせ",
        "ご不明点",
        "注文番号",
        "http://",
        "https://",
    ]
    return any(k in s2 for k in ng_keywords)


# ----------------------------
# quoted-printable helpers (for URL recovery)
# ----------------------------

def _qp_normalize_for_url_scan(s: str) -> str:
    """
    quoted-printable のメール本文向け軽量正規化:
    - soft line break (=\r\n / =\n) を除去
    - quopri decode を試す
    - 失敗時は最低限 =3D を戻す
    """
    if not s:
        return ""

    raw = s.replace("=\r\n", "").replace("=\n", "")

    try:
        decoded = quopri.decodestring(raw.encode("utf-8", errors="ignore")).decode(
            "utf-8", errors="ignore"
        )
        if decoded:
            return decoded
    except Exception:
        pass

    return raw.replace("=3D", "=")


def _canonical_order_url_from_hash(order_hash: str) -> str:
    h = re.sub(r"\s+", "", order_hash or "").lower()
    return f"https://account.life-tuning-online.com/orders/{h}?locale=ja-JP"


# ----------------------------
# source / map URL extraction
# ----------------------------

def _extract_source_url(text: str, soup: Optional[BeautifulSoup], raw_rfc822: Optional[str]) -> Optional[str]:
    """
    source_url は account.life-tuning-online.com/orders/<hash>?locale=ja-JP を返す。

    優先順位:
      1) 本文/HTML/raw MIME に account URL があればそれを採用（canonical化）
      2) life-tuning-online.com/.../orders/<hash>/authenticate?... から hash を抽出して canonical URL を生成
      3) 取れなければ None
    """
    html_raw = str(soup) if soup else ""

    # raw MIME を最優先に見る（quoted-printableのフルURLが残っているため）
    scan_targets = [
        _qp_normalize_for_url_scan(raw_rfc822 or ""),
        _qp_normalize_for_url_scan(text or ""),
        _qp_normalize_for_url_scan(html_raw or ""),
    ]

    # 1) account URL 直接
    for src in scan_targets:
        m = _ACCOUNT_ORDER_RE.search(src)
        if m:
            return _canonical_order_url_from_hash(m.group(1))

    # 2) Shopify auth URL -> hash 抽出 -> canonical
    for src in scan_targets:
        m = _SHOPIFY_AUTH_HASH_RE.search(src)
        if not m:
            continue

        raw_hash = m.group(1)
        # quoted-printable の = 改行混入対策
        order_hash = raw_hash.replace("=", "").replace("\r", "").replace("\n", "")
        order_hash = order_hash.strip()

        if re.fullmatch(r"[a-f0-9]{20,}", order_hash, re.I):
            return _canonical_order_url_from_hash(order_hash)

    return None


def _extract_map_url(text: str, soup: Optional[BeautifulSoup], raw_rfc822: Optional[str]) -> Optional[str]:
    """
    maps.app.goo.gl のURLを抽出。
    今回は最小修正として Event.address に格納する想定。
    """
    html_raw = str(soup) if soup else ""
    scan_targets = [
        _qp_normalize_for_url_scan(raw_rfc822 or ""),
        _qp_normalize_for_url_scan(text or ""),
        _qp_normalize_for_url_scan(html_raw or ""),
    ]

    for src in scan_targets:
        m = _MAP_URL_RE.search(src)
        if m:
            return _normalize_url(m.group(0))
    return None


def _extract_location_name(text: str) -> Optional[str]:
    """
    maps URL の直前行を会場名として拾う。
    """
    raw_lines = [ln.strip() for ln in (text or "").splitlines()]

    for i, ln in enumerate(raw_lines):
        if "maps.app.goo.gl/" in ln or "google.com/maps" in ln:
            for j in range(i - 1, max(-1, i - 8), -1):
                prev = re.sub(r"\s+", " ", raw_lines[j]).strip()
                if not prev:
                    continue
                if "http://" in prev or "https://" in prev:
                    continue
                if prev.startswith("(") and prev.endswith(")"):
                    continue
                if any(k in prev for k in ["ご不明点", "お問い合わせ", "注文番号"]):
                    continue
                return prev

    return first_non_empty(
        extract_label_value(text, "会場"),
        extract_label_value(text, "場所"),
    )


# ----------------------------
# order number / price extraction
# ----------------------------

def _extract_order_number(text: str) -> Optional[str]:
    v = first_non_empty(
        extract_label_value(text, "注文番号"),
        extract_label_value(text, "注文番号（Order Number）"),
    )
    if v:
        m = _ORDER_NO_RE.search(v)
        if m:
            return m.group(1).upper()

    m = re.search(r"注文番号[^\n#]*#\s*(LTO\d{4,})", text or "", re.I)
    if m:
        return m.group(1).upper()

    m = _ORDER_NO_RE.search(text or "")
    if m:
        return m.group(1).upper()

    return None


def _extract_price_option(text: str) -> Optional[list[str]]:
    """
    例:
      ¥3,500
      3,500円
    を option_menus に入れる。
    """
    lines = _normalize_lines(text)

    # 「レッスン」周辺を優先探索
    for i, ln in enumerate(lines):
        if ln == "レッスン":
            for j in range(i + 1, min(i + 10, len(lines))):
                cand = lines[j]
                m = _YEN_RE.search(cand)
                if m:
                    return [m.group(0).replace("￥", "¥").replace(" ", "")]
                m2 = re.search(r"(\d[\d,]*)\s*円", cand)
                if m2:
                    return [f"¥{m2.group(1)}"]

    # 全文 fallback
    m = _YEN_RE.search(text or "")
    if m:
        return [m.group(0).replace("￥", "¥").replace(" ", "")]
    m2 = re.search(r"(\d[\d,]*)\s*円", text or "")
    if m2:
        return [f"¥{m2.group(1)}"]

    return None


# ----------------------------
# date/title extraction
# ----------------------------

def _extract_date_title_from_order_section(text: str) -> Tuple[Optional[datetime], Optional[str], bool]:
    """
    LIFE TUNING の注文概要から date/title を抽出する。
    対応パターン:
      A) 1行型:
         2/20（金）19:30 ～ 温め、ゆるめるヨガ × 1
      B) 複数行型（時間あり）:
         2/20（金）19:30
         温め、ゆるめるヨガ
         × 1
      C) 複数行型（時間なし、同じ行にタイトル前半あり）:
         1月17日（土）Inner Change, New Beginning
         ～変化が開く、新しい私～【上崎菜保子 /EMIANA コラボレーションクラス】 × 1
    戻り値:
      (date, title, time_unknown)
    """
    lines = _normalize_lines(text)

    # 0) まず1行型を全体探索（時間あり）
    for ln in lines:
        m = _ONE_LINE_ITEM_RE.match(ln)
        if m:
            month = int(m.group(1))
            day = int(m.group(2))
            hour = int(m.group(3))
            minute = int(m.group(4))
            title = m.group(5).strip()
            y = _guess_year(month, day)
            try:
                dt = datetime(y, month, day, hour, minute)
                return dt, title, False
            except ValueError:
                pass

    # 1) 注文概要/レッスン付近を優先探索
    anchors = []
    for i, ln in enumerate(lines):
        if ("注文概要" in ln) or ("Order Details" in ln) or (ln == "レッスン"):
            anchors.append(i)

    ranges: List[Tuple[int, int]] = []
    if anchors:
        for i in anchors:
            ranges.append((max(0, i - 5), min(len(lines), i + 50)))
    else:
        ranges.append((0, len(lines)))

    for start, end in ranges:
        for i in range(start, end):
            ln = lines[i]

            # A) 時間あり日付行のみ（次行以降にタイトル）
            m = _DATE_LINE_MMDD_TIME_RE.match(ln)
            if m:
                month = int(m.group(1))
                day = int(m.group(2))
                hour = int(m.group(3))
                minute = int(m.group(4))
                y = _guess_year(month, day)

                try:
                    dt = datetime(y, month, day, hour, minute)
                except ValueError:
                    dt = None

                if dt:
                    parts: List[str] = []
                    for j in range(i + 1, min(i + 8, end)):
                        cand_raw = lines[j]
                        cand = cand_raw.strip("～〜- ").strip()

                        # ×1 のみなら終端
                        if re.fullmatch(r"[×xX]\s*\d+", cand_raw):
                            break

                        if _is_noise_title_line(cand):
                            continue

                        cand = re.sub(r"\s*[×xX]\s*\d+\s*$", "", cand).strip()
                        cand = cand.strip("～〜- ").strip()
                        if cand and not _is_noise_title_line(cand):
                            parts.append(cand)

                        if "×" in cand_raw:
                            break

                    title = " ".join([p for p in parts if p]).strip() or None
                    return dt, title, False

            # B) 時間なし日付行だけ（次行以降にタイトル）
            m2 = _DATE_LINE_JP_RE.match(ln)
            if m2:
                month = int(m2.group(1))
                day = int(m2.group(2))
                y = _guess_year(month, day)

                try:
                    dt = datetime(y, month, day, 12, 0)
                except ValueError:
                    dt = None

                if dt:
                    parts: List[str] = []
                    for j in range(i + 1, min(i + 8, end)):
                        cand_raw = lines[j]
                        cand = cand_raw.strip("～〜- ").strip()

                        if re.fullmatch(r"[×xX]\s*\d+", cand_raw):
                            break

                        if _is_noise_title_line(cand):
                            continue

                        cand = re.sub(r"\s*[×xX]\s*\d+\s*$", "", cand).strip()
                        cand = cand.strip("～〜- ").strip()
                        if cand and not _is_noise_title_line(cand):
                            parts.append(cand)

                        if "×" in cand_raw:
                            break

                    title = " ".join([p for p in parts if p]).strip() or None
                    return dt, title, True

            # C) 時間なし + 同一行にタイトル前半あり（1/17 パターン）
            m3 = _JP_DATE_WITH_TITLE_HEAD_RE.match(ln)
            if m3:
                month = int(m3.group(1))
                day = int(m3.group(2))
                tail = m3.group(3).strip()  # 同じ行のタイトル前半
                y = _guess_year(month, day)

                try:
                    dt = datetime(y, month, day, 12, 0)
                except ValueError:
                    dt = None

                if dt:
                    parts: List[str] = []

                    if tail and not _is_noise_title_line(tail):
                        # 同じ行で "×1" が付いてたら削る
                        tail = re.sub(r"\s*[×xX]\s*\d+\s*$", "", tail).strip()
                        tail = tail.strip("～〜- ").strip()
                        if tail and not _is_noise_title_line(tail):
                            parts.append(tail)

                    # 次行に続きタイトル（～... ×1）があることが多い
                    for j in range(i + 1, min(i + 5, end)):
                        cand_raw = lines[j]
                        cand = cand_raw.strip()

                        # 数量だけの行
                        if re.fullmatch(r"[×xX]\s*\d+", cand):
                            break

                        # タイトル続きとして許容（先頭に ～ が来ることがある）
                        cand2 = cand.strip("～〜- ").strip()
                        if _is_noise_title_line(cand2) and ("×" not in cand):
                            break

                        cand2 = re.sub(r"\s*[×xX]\s*\d+\s*$", "", cand2).strip()
                        if cand2 and not _is_noise_title_line(cand2):
                            parts.append(cand2)

                        if "×" in cand_raw:
                            break

                    title = " ".join([p for p in parts if p]).strip() or None
                    return dt, title, True

    return None, None, False


# ----------------------------
# main parser
# ----------------------------

def parse_life_tuning(msg: GmailMessage) -> Optional[Event]:
    raw = msg.text_plain or msg.text_html or msg.raw_rfc822 or ""
    if not raw:
        return None

    text, soup = _to_text_and_soup(msg)
    if not text and msg.raw_rfc822:
        # 最低限 raw_rfc822 をテキストとして使うフォールバック
        text = msg.raw_rfc822

    if not text:
        return None

    # 注文概要から date/title を優先抽出
    parsed_date, parsed_title, parsed_time_unknown = _extract_date_title_from_order_section(text)

    date = parsed_date
    time_unknown = parsed_time_unknown
    confidence = 1.0 if parsed_date else 0.7

    # 最終fallback（日付だけは最低限取りたい）
    if not date:
        date = parse_first_datetime(text)
        if not date:
            return None
        time_unknown = False
        confidence = 0.6

    title = first_non_empty(
        parsed_title,
        extract_label_value(text, "商品名"),
        extract_label_value(text, "イベント"),
        msg.subject,
        "LIFE TUNING DAYS",
    )

    reservation_id = _extract_order_number(text)

    # source_url（order URL）
    source_url = _extract_source_url(text, soup, msg.raw_rfc822)

    # 会場名 & 地図URL（最小修正として address に maps URL を入れる）
    location_name = _extract_location_name(text)
    map_url = _extract_map_url(text, soup, msg.raw_rfc822)

    # 金額（税込み）を option_menus に入れる
    option_menus = _extract_price_option(text)

    return Event(
        provider="life_tuning",
        title=title or "LIFE TUNING DAYS",
        date=date,
        location_name=location_name,
        address=map_url,  # maps URL を地図情報として保持（最小修正）
        instructor=None,
        reservation_id=reservation_id,
        source_url=source_url,
        option_menus=option_menus,
        confidence=confidence,
        time_unknown=time_unknown,
    )
