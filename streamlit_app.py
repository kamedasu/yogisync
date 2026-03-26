from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import plotly.express as px
from google.oauth2 import service_account
from googleapiclient.discovery import build


# ============================================================
# Config / Secrets helpers
# ============================================================

def _get_secret(key: str) -> Any:
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return None


def _get_calendar_id() -> str:
    cid = os.getenv("YOGISYNC_CALENDAR_ID") or _get_secret("YOGISYNC_CALENDAR_ID")
    if not cid:
        raise RuntimeError("YOGISYNC_CALENDAR_ID is not configured.")
    return str(cid).strip()


def _get_service_account_info() -> Dict[str, Any]:
    """
    Streamlit Cloud: secrets.toml に gcp_service_account を置く
      [gcp_service_account]
      type="service_account"
      ...
    もしくはローカル用に GOOGLE_SERVICE_ACCOUNT_PATH でJSONパス指定
    """
    sa = _get_secret("gcp_service_account")
    if sa:
        return dict(sa)

    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH")
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    raise RuntimeError(
        "Service account JSON is not configured. "
        "Set st.secrets['gcp_service_account'] or GOOGLE_SERVICE_ACCOUNT_PATH."
    )


def get_calendar_service():
    info = _get_service_account_info()
    scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    return build("calendar", "v3", credentials=creds)


# ============================================================
# Instructor aliases (dictionary)
# ============================================================

def _normalize_spaces(s: str) -> str:
    s = (s or "").replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _strip_title_suffix(name: str) -> str:
    n = _normalize_spaces(name)
    n = re.sub(r"[（(].*?[）)]", "", n).strip()
    n = re.sub(r"(先生|さん|様|氏)$", "", n).strip()
    n = re.sub(r"(?i)\s*(san|sensei)$", "", n).strip()
    return n


def _norm_key(s: str) -> str:
    """
    辞書キー照合用の正規化：
    - 全角スペース→半角
    - 余分な空白潰し
    - 末尾敬称除去
    - 小文字化（英字系の揺れ吸収）
    """
    x = _strip_title_suffix(s)
    x = _normalize_spaces(x)
    return x.lower()


def load_instructor_aliases() -> Dict[str, str]:
    """
    instructor_aliases.json を読み込む（任意）
    - 環境変数 INSTRUCTOR_ALIAS_PATH があればそれを優先
    - なければ ./instructor_aliases.json
    """
    path = os.getenv("INSTRUCTOR_ALIAS_PATH") or "instructor_aliases.json"
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        # 正規化キーにして保持
        out: Dict[str, str] = {}
        for k, v in data.items():
            if not k or not v:
                continue
            out[_norm_key(str(k))] = _strip_title_suffix(str(v))
        return out
    except Exception:
        return {}


def apply_alias(name: str, aliases: Dict[str, str]) -> str:
    key = _norm_key(name)
    if key in aliases:
        return aliases[key]
    return name


# ============================================================
# Google Calendar fetch
# ============================================================

@st.cache_data(ttl=300, show_spinner=False)
def fetch_events(calendar_id: str) -> list[dict[str, Any]]:
    service = get_calendar_service()

    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=365)).isoformat()
    time_max = (now + timedelta(days=365)).isoformat()

    events: list[dict[str, Any]] = []
    page_token: Optional[str] = None

    while True:
        resp = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        events.extend(resp.get("items", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return events


# ============================================================
# Parsing helpers
# ============================================================

def parse_description(desc: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    if not desc:
        return meta

    for line in desc.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower()
        v = v.strip()
        if k:
            meta[k] = v
    return meta


def extract_datetime(item: dict[str, Any]) -> Optional[datetime]:
    start = (item.get("start") or {})
    if "dateTime" in start:
        s = start["dateTime"]
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return None
    if "date" in start:
        s = start["date"]
        try:
            d = datetime.fromisoformat(s).date()
            # all-dayは適当に昼固定（集計用）
            return datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def extract_instructor_raw(summary: str, meta: dict[str, str]) -> str:
    if meta.get("instructor"):
        return meta["instructor"].strip() or "不明"

    # summary: "... - instructor"
    if " - " in summary:
        tail = summary.rsplit(" - ", 1)[-1].strip()
        if tail and len(tail) <= 60:
            return tail

    return "不明"


def intensity_label(text: str) -> str:
    hard_keywords = ["power", "ashtanga", "vinyasa", "core", "筋", "強度", "ハード", "flow"]
    soft_keywords = ["restorative", "yin", "relax", "gentle", "リラックス", "やさしい", "陰"]

    lower = (text or "").lower()
    hard = sum(1 for k in hard_keywords if k.lower() in lower)
    soft = sum(1 for k in soft_keywords if k.lower() in lower)

    if hard > soft:
        return "ハード寄り"
    if soft > hard:
        return "リラックス寄り"
    return "バランス"


def canonicalize_instructor(name: str, aliases: Dict[str, str]) -> str:
    n = _strip_title_suffix(name)

    # 記号揺れを軽く正規化
    n = n.replace("・", " ").replace("／", "/")
    n = _normalize_spaces(n)

    # 辞書（エイリアス）を先に当てる
    n = apply_alias(n, aliases)

    # 固定マッピング（必要なら増やす）
    # 上崎菜保子の揺れを吸収
    if re.search(r"上崎\s*菜保子", n):
        return "上崎菜保子"

    # NaOKO 揺れ
    if re.fullmatch(r"(?i)naoko", n):
        return "NaOKO"

    # ほのか先生 → ほのか（敬称除去で落ちるが念のため）
    if re.fullmatch(r"ほのか", n):
        return "ほのか"

    # ★重要：緒方さと美 は “と” を含むが1人名
    # ここで分割はしない（分割ロジック側で「と」を区切り扱いしない）
    return n


def split_instructors(raw: str, aliases: Dict[str, str]) -> list[str]:
    """
    「ゆりこ・かほ」「朱音＆ゆりこ」などを分割して別々に数える。
    注意：日本語の「と」は人名内部に含まれ得る（例：緒方さと美）ので区切り扱いしない。
    """
    if not raw:
        return []

    s = _normalize_spaces(raw)
    s = s.replace("＆", "&")

    # 区切り：・ / & / 、 / , / + / / / and
    s = re.sub(r"\s*(?:・|/|&|,|、|\+)\s*", "|", s)
    s = re.sub(r"(?i)\s+and\s+", "|", s)

    parts = [p.strip() for p in s.split("|") if p.strip()]

    cleaned: list[str] = []
    for p in parts:
        c = canonicalize_instructor(p, aliases)
        if c and c != "不明":
            cleaned.append(c)

    # 同一イベント内の重複は潰す（ほのか/ほのか先生問題など）
    dedup: list[str] = []
    seen = set()
    for x in cleaned:
        if x not in seen:
            seen.add(x)
            dedup.append(x)
    return dedup


# ============================================================
# Area extraction (Tokyo wards / Kanagawa cities)
# ============================================================

_TOKYO_HINTS = {
    "渋谷": "東京:渋谷区",
    "新宿": "東京:新宿区",
    "港": "東京:港区",
    "中央": "東京:中央区",
    "目黒": "東京:目黒区",
    "世田谷": "東京:世田谷区",
    "杉並": "東京:杉並区",
    "中野": "東京:中野区",
    "品川": "東京:品川区",
    "台東": "東京:台東区",
    "千代田": "東京:千代田区",
    "文京": "東京:文京区",
    "豊島": "東京:豊島区",
    "江東": "東京:江東区",
    "大田": "東京:大田区",
}


def extract_area(location: str) -> str:
    if not location:
        return "その他"

    s = str(location).strip()

    if "東京都" in s:
        m = re.search(r"東京都.*?([^\s　]+?区)", s)
        if m:
            return f"東京:{m.group(1)}"
        m = re.search(r"東京都.*?([^\s　]+?市)", s)
        if m:
            return f"東京:{m.group(1)}"
        for k, v in _TOKYO_HINTS.items():
            if k in s:
                return v
        return "東京:不明"

    if "神奈川県" in s:
        m = re.search(r"神奈川県.*?([^\s　]+?市[^\s　]*?区)", s)
        if m:
            return f"神奈川:{m.group(1)}"
        m = re.search(r"神奈川県.*?([^\s　]+?市)", s)
        if m:
            return f"神奈川:{m.group(1)}"
        m = re.search(r"神奈川県.*?([^\s　]+?区)", s)
        if m:
            return f"神奈川:{m.group(1)}"
        return "神奈川:不明"

    for k, v in _TOKYO_HINTS.items():
        if k in s:
            return v

    return "その他"


def make_area_pie(df: pd.DataFrame) -> pd.DataFrame:
    counts = df["area"].value_counts()
    top_n = 12
    top = counts.head(top_n)
    others = counts.iloc[top_n:].sum()
    if others > 0:
        top = pd.concat([top, pd.Series({"その他(まとめ)": others})])

    pie_df = top.rename_axis("area").reset_index(name="count")
    pie_df["pct"] = (pie_df["count"] / pie_df["count"].sum() * 100).round(1)
    return pie_df


# ============================================================
# Build dataframe
# ============================================================

def build_dataframe(events: list[dict[str, Any]], aliases: Dict[str, str]) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    rows = []

    for item in events:
        dt = extract_datetime(item)
        if dt is None:
            continue

        summary = item.get("summary", "") or ""
        desc = item.get("description", "") or ""
        meta = parse_description(desc)

        location = item.get("location") or meta.get("address") or meta.get("location_name") or "未設定"
        provider = meta.get("provider") or "unknown"
        confidence = meta.get("confidence", "")

        try:
            confidence_val = float(confidence)
        except Exception:
            confidence_val = None

        instructor_raw = extract_instructor_raw(summary, meta)
        instructors = split_instructors(instructor_raw, aliases)
        if not instructors:
            instructors = ["不明"]

        rows.append(
            {
                "date": dt,
                "summary": summary,
                "provider": provider,
                "instructor_raw": instructor_raw,
                "instructors": instructors,  # list
                "location": str(location),
                "area": extract_area(str(location)),
                "source_url": meta.get("source_url", ""),
                "confidence": confidence_val,
                "intensity": intensity_label(f"{summary}\n{desc}"),
                "is_recent_90d": dt >= now - timedelta(days=90),
                "is_this_month": (dt.year == now.year and dt.month == now.month),
            }
        )

    if not rows:
        return pd.DataFrame(
            columns=[
                "date",
                "summary",
                "provider",
                "instructor_raw",
                "instructors",
                "location",
                "area",
                "source_url",
                "confidence",
                "intensity",
                "is_recent_90d",
                "is_this_month",
            ]
        )

    df = pd.DataFrame(rows).sort_values("date", ascending=False)
    df["provider"] = df["provider"].replace("", "unknown").fillna("unknown")
    df["location"] = df["location"].replace("", "未設定").fillna("未設定")
    df["area"] = df["area"].replace("", "その他").fillna("その他")
    df["intensity"] = df["intensity"].replace("", "バランス").fillna("バランス")
    df["instructor_raw"] = df["instructor_raw"].replace("", "不明").fillna("不明")
    return df


def build_instructor_table(df: pd.DataFrame) -> pd.DataFrame:
    ex = df.copy()
    ex = ex.explode("instructors", ignore_index=True)
    ex["instructor"] = ex["instructors"].fillna("不明").astype(str)
    ex.drop(columns=["instructors"], inplace=True)
    return ex


def inactive_instructors_with_last_date(ex: pd.DataFrame, days: int = 90) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    valid = ex[ex["instructor"] != "不明"].copy()
    if valid.empty:
        return pd.DataFrame(columns=["instructor", "last_date"])

    last_dates = valid.groupby("instructor")["date"].max().reset_index()
    last_dates.rename(columns={"date": "last_date"}, inplace=True)

    inactive = last_dates[last_dates["last_date"] < cutoff].sort_values("last_date", ascending=True)
    inactive["last_date"] = inactive["last_date"].dt.tz_convert("Asia/Tokyo").dt.strftime("%Y-%m-%d")
    return inactive.reset_index(drop=True)


def top_instructors(ex: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    valid = ex[ex["instructor"] != "不明"].copy()
    if valid.empty:
        return pd.DataFrame(columns=["instructor", "count"])
    counts = valid["instructor"].value_counts().head(n).rename_axis("instructor").reset_index(name="count")
    return counts


# ============================================================
# UI
# ============================================================

def main() -> None:
    load_dotenv()

    st.set_page_config(page_title="YogiSync Phase2 Dashboard", layout="wide")
    st.title("YogiSync Phase2 | 私のヨガ活動ダッシュボード")

    # --- aliases load + sidebar override ---
    base_aliases = load_instructor_aliases()

    st.sidebar.header("講師名ゆれ辞書（任意）")
    st.sidebar.caption("`instructor_aliases.json` を置くか、ここでJSONをアップロードすると表記ゆれを吸収できます。")

    upload = st.sidebar.file_uploader("辞書JSONをアップロード（任意）", type=["json"])
    if upload is not None:
        try:
            data = json.load(upload)
            if isinstance(data, dict):
                merged = dict(base_aliases)
                for k, v in data.items():
                    if k and v:
                        merged[_norm_key(str(k))] = _strip_title_suffix(str(v))
                aliases = merged
                st.sidebar.success("アップロード辞書を適用しました")
            else:
                aliases = base_aliases
                st.sidebar.warning("JSONがdict形式ではないため無視しました")
        except Exception:
            aliases = base_aliases
            st.sidebar.warning("JSONの読み込みに失敗しました（無視）")
    else:
        aliases = base_aliases

    with st.sidebar.expander("現在の辞書（読み込み後）", expanded=False):
        if aliases:
            # 表示用にキーを戻して見やすく
            st.json({k: v for k, v in list(aliases.items())[:200]})
        else:
            st.write("辞書なし（未設定）")

    try:
        calendar_id = _get_calendar_id()
        events = fetch_events(calendar_id)
    except Exception as e:
        st.error(str(e))
        st.stop()

    df = build_dataframe(events, aliases)
    if df.empty:
        st.warning("イベントが取得できませんでした。カレンダー共有設定とCalendar IDを確認してください。")
        st.stop()

    ex = build_instructor_table(df)

    # KPIs
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("総レッスン数", len(df))
    col2.metric("当月", int(df["is_this_month"].sum()))
    col3.metric("直近90日", int(df["is_recent_90d"].sum()))
    col4.metric("ユニーク講師数", int(ex[ex["instructor"] != "不明"]["instructor"].nunique()))

    # 1) 講師別：よく受けている
    st.subheader("よく受けている講師（上位）")
    top_df = top_instructors(ex, n=20)
    if top_df.empty:
        st.info("講師情報のあるイベントがありません。")
    else:
        fig = px.bar(top_df, x="instructor", y="count")
        fig.update_layout(xaxis_title="", yaxis_title="回数")
        st.plotly_chart(fig, use_container_width=True)

    # 2) 最近受けていない講師（いつから受けてないか）
    st.subheader("最近受けていない講師（直近90日で0回 + 最終受講日）")
    inactive_df = inactive_instructors_with_last_date(ex, days=90)
    if inactive_df.empty:
        st.write("該当なし")
    else:
        st.dataframe(inactive_df, use_container_width=True, hide_index=True)

    # 3) レッスン傾向（強度）
    st.subheader("レッスン傾向（強度のざっくり分類）")
    inten = df["intensity"].value_counts().rename_axis("intensity").reset_index(name="count")
    fig2 = px.bar(inten, x="intensity", y="count")
    fig2.update_layout(xaxis_title="", yaxis_title="回数")
    st.plotly_chart(fig2, use_container_width=True)

    # 4) 受講エリア（円グラフ％）
    st.subheader("受講エリア比率（東京都=区別 / 神奈川=市・区別）")
    pie_df = make_area_pie(df)
    fig3 = px.pie(
        pie_df,
        names="area",
        values="count",
        hover_data=["pct"],
        labels={"pct": "%"},
    )
    fig3.update_traces(textposition="inside", textinfo="percent+label")
    st.plotly_chart(fig3, use_container_width=True)

    with st.expander("エリア内訳（件数）"):
        area_counts = df["area"].value_counts().rename_axis("area").reset_index(name="count")
        st.dataframe(area_counts, use_container_width=True, hide_index=True)

    # 5) データプレビュー（講師ごとに展開）だけ残す
    with st.expander("データプレビュー（講師ごとに展開）"):
        st.dataframe(
            ex[["date", "instructor", "area", "location", "summary", "provider"]],
            use_container_width=True,
        )


if __name__ == "__main__":
    main()
