from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pydeck as pdk
import streamlit as st
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

load_dotenv()


def _get_secret(key: str, default: Any = None) -> Any:
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def _load_service_account_info() -> dict[str, Any]:
    secret = _get_secret("gcp_service_account")
    if secret:
        return dict(secret)

    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH", "secrets/service_account.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    raise RuntimeError(
        "Service account JSON is not configured. "
        "Set st.secrets['gcp_service_account'] or GOOGLE_SERVICE_ACCOUNT_PATH."
    )


def _get_calendar_id() -> str:
    calendar_id = _get_secret("YOGISYNC_CALENDAR_ID") or os.getenv("YOGISYNC_CALENDAR_ID", "")
    if not calendar_id:
        raise RuntimeError("YOGISYNC_CALENDAR_ID is not configured.")
    return calendar_id


@st.cache_resource(show_spinner=False)
def get_calendar_service():
    info = _load_service_account_info()
    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )
    return build("calendar", "v3", credentials=creds)


@st.cache_data(ttl=300, show_spinner=False)
def fetch_events(calendar_id: str, days_back: int = 365) -> list[dict[str, Any]]:
    service = get_calendar_service()
    time_min = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()

    items: list[dict[str, Any]] = []
    page_token = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                singleEvents=True,
                orderBy="startTime",
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        items.extend(resp.get("items", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return items


def parse_description(desc: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in (desc or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip().lower()] = value.strip()
    return result


def extract_datetime(event: dict[str, Any]) -> datetime | None:
    start = event.get("start", {})
    dt = start.get("dateTime")
    if dt:
        return datetime.fromisoformat(dt.replace("Z", "+00:00"))

    all_day = start.get("date")
    if all_day:
        return datetime.fromisoformat(all_day).replace(tzinfo=timezone.utc)

    return None


def extract_instructor(summary: str, meta: dict[str, str]) -> str:
    for key in ("instructor", "teacher", "講師"):
        if meta.get(key):
            return meta[key]

    if " - " in summary:
        tail = summary.rsplit(" - ", 1)[-1].strip()
        if tail:
            return tail

    m = re.search(r"(?:講師|Instructor)[:：]\s*([^\n\r]+)", summary, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return "不明"


def intensity_label(text: str) -> str:
    hard_keywords = ["power", "ashtanga", "vinyasa", "core", "筋", "強度", "ハード", "flow"]
    soft_keywords = ["restorative", "yin", "relax", "gentle", "リラックス", "やさしい", "陰"]
    lower = text.lower()

    hard = sum(1 for k in hard_keywords if k.lower() in lower)
    soft = sum(1 for k in soft_keywords if k.lower() in lower)

    if hard > soft:
        return "ハード寄り"
    if soft > hard:
        return "リラックス寄り"
    return "バランス"


def build_dataframe(events: list[dict[str, Any]]) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    rows = []
    for item in events:
        dt = extract_datetime(item)
        if dt is None:
            continue

        summary = item.get("summary", "")
        desc = item.get("description", "")
        meta = parse_description(desc)
        provider = meta.get("provider") or "unknown"
        location = item.get("location") or meta.get("address") or "未設定"
        confidence = meta.get("confidence", "")

        try:
            confidence_val = float(confidence)
        except Exception:
            confidence_val = None

        rows.append(
            {
                "date": dt,
                "summary": summary,
                "provider": provider,
                "instructor": extract_instructor(summary, meta),
                "location": location,
                "source_url": meta.get("source_url", ""),
                "confidence": confidence_val,
                "intensity": intensity_label(f"{summary}\n{desc}"),
                "is_recent_90d": dt >= now - timedelta(days=90),
                "is_this_month": dt.year == now.year and dt.month == now.month,
            }
        )

    if not rows:
        return pd.DataFrame(
            columns=[
                "date",
                "summary",
                "provider",
                "instructor",
                "location",
                "source_url",
                "confidence",
                "intensity",
                "is_recent_90d",
                "is_this_month",
            ]
        )

    df = pd.DataFrame(rows)
    df["provider"] = df["provider"].replace("", "unknown").fillna("unknown")
    df["instructor"] = df["instructor"].replace("", "不明").fillna("不明")
    df["location"] = df["location"].replace("", "未設定").fillna("未設定")
    return df.sort_values("date", ascending=False)


def _normalize_location_for_geocode(loc: str) -> str:
    loc = (loc or "").strip()
    if not loc or loc == "未設定":
        return ""

    # ざっくり：日本（東京/神奈川）に寄せる
    if any(k in loc for k in ["東京都", "渋谷", "新宿", "港区", "目黒", "品川", "世田谷", "中央区", "千代田", "台東", "文京", "豊島"]):
        return f"{loc}, Tokyo, Japan"
    if any(k in loc for k in ["神奈川", "横浜", "川崎", "鎌倉", "湘南", "藤沢"]):
        return f"{loc}, Kanagawa, Japan"

    # 明示がなければ Japan を付ける
    if "Japan" not in loc and "日本" not in loc:
        return f"{loc}, Japan"
    return loc


@st.cache_data(ttl=86400, show_spinner=False)
def geocode_location(location_name: str) -> dict[str, float] | None:
    q = _normalize_location_for_geocode(location_name)
    if not q:
        return None

    # Nominatim(OpenStreetMap) へ標準ライブラリだけで問い合わせ
    # ※レート制限があるので、上位N件だけ + cache で運用する前提
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": "1"}
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "yogisync-phase2-dashboard/1.0 (contact: local)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data:
            return None
        return {"lat": float(data[0]["lat"]), "lon": float(data[0]["lon"])}
    except Exception:
        return None


def build_rule_based_comment(df: pd.DataFrame) -> str:
    if df.empty:
        return "対象期間にイベントがありません。"

    total = len(df)
    recent = int(df["is_recent_90d"].sum())
    this_month = int(df["is_this_month"].sum())

    top_provider = df["provider"].value_counts().head(1)
    top_instructor = df[df["instructor"] != "不明"]["instructor"].value_counts().head(1)
    intensity = df["intensity"].value_counts(normalize=True)

    lines = [
        f"過去データ全体で {total} 件、直近90日で {recent} 件、当月で {this_month} 件の受講があります。",
    ]

    if not top_provider.empty:
        lines.append(f"最も多いproviderは {top_provider.index[0]}（{int(top_provider.iloc[0])}件）です。")

    if not top_instructor.empty:
        lines.append(f"最も受講が多い講師は {top_instructor.index[0]}（{int(top_instructor.iloc[0])}件）です。")

    hard_ratio = intensity.get("ハード寄り", 0.0)
    soft_ratio = intensity.get("リラックス寄り", 0.0)
    if hard_ratio >= 0.45:
        lines.append("全体としてハード寄りのクラス比率が高めです。")
    elif soft_ratio >= 0.45:
        lines.append("全体としてリラックス寄りのクラス比率が高めです。")
    else:
        lines.append("強度傾向はバランス型です。")

    return "\n".join(lines)


def maybe_generate_llm_comment(rule_comment: str, df: pd.DataFrame) -> str:
    api_key = _get_secret("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key or df.empty:
        return rule_comment

    try:
        from openai import OpenAI

        model = _get_secret("OPENAI_MODEL") or os.getenv("OPENAI_MODEL", "gpt-5-mini")
        provider_stats = df["provider"].value_counts().to_dict()
        instructor_stats = df[df["instructor"] != "不明"]["instructor"].value_counts().head(10).to_dict()
        intensity_stats = df["intensity"].value_counts(normalize=True).round(3).to_dict()

        prompt = (
            "あなたはヨガ活動の分析アシスタントです。\n"
            "以下の統計をもとに、120文字以内で具体的な振り返りコメントを日本語で作ってください。\n"
            f"rule_comment={rule_comment}\n"
            f"provider_stats={provider_stats}\n"
            f"instructor_stats={instructor_stats}\n"
            f"intensity_stats={intensity_stats}\n"
        )

        client = OpenAI(api_key=api_key)
        resp = client.responses.create(
            model=model,
            input=prompt,
            max_output_tokens=200,
        )
        text = resp.output_text.strip()
        return text or rule_comment
    except Exception:
        return rule_comment


def render_tokyo_area_map(map_df: pd.DataFrame) -> None:
    # 東京駅あたり
    default_center = {"lat": 35.681236, "lon": 139.767125}

    if not map_df.empty:
        center_lat = float(map_df["lat"].mean())
        center_lon = float(map_df["lon"].mean())
    else:
        center_lat, center_lon = default_center["lat"], default_center["lon"]

    view_state = pdk.ViewState(
        latitude=center_lat,
        longitude=center_lon,
        zoom=10.5,
        pitch=0,
        bearing=0,
    )

    layer = pdk.Layer(
        "ScatterplotLayer",
        data=map_df,
        get_position="[lon, lat]",
        get_radius=200,
        pickable=True,
    )

    tooltip = {"text": "{location}\n{count}件"}

    deck = pdk.Deck(
        map_style="mapbox://styles/mapbox/dark-v10",
        initial_view_state=view_state,
        layers=[layer],
        tooltip=tooltip,
    )
    st.pydeck_chart(deck, use_container_width=True)


def main() -> None:
    st.set_page_config(page_title="YogiSync Phase2 Dashboard", layout="wide")
    st.title("YogiSync Phase2 | 私のヨガ活動ダッシュボード")

    try:
        calendar_id = _get_calendar_id()
        events = fetch_events(calendar_id)
    except Exception as e:
        st.error(str(e))
        st.stop()

    df = build_dataframe(events)

    if df.empty:
        st.warning("イベントが取得できませんでした。カレンダー共有設定とCalendar IDを確認してください。")
        st.stop()

    col1, col2, col3 = st.columns(3)
    col1.metric("総レッスン数", len(df))
    col2.metric("当月", int(df["is_this_month"].sum()))
    col3.metric("直近90日", int(df["is_recent_90d"].sum()))

    st.subheader("provider別レッスン数")
    provider_counts = df["provider"].value_counts().rename_axis("provider").to_frame("count")
    st.bar_chart(provider_counts)

    st.subheader("講師別レッスン数")
    instructor_counts = (
        df[df["instructor"] != "不明"]["instructor"].value_counts().rename_axis("instructor").to_frame("count")
    )
    if instructor_counts.empty:
        st.info("講師情報のあるイベントがありません。")
    else:
        st.bar_chart(instructor_counts.head(20))

    st.subheader("最近受けていない講師（直近90日で0回）")
    all_instructors = set(df[df["instructor"] != "不明"]["instructor"].tolist())
    recent_instructors = set(df[(df["instructor"] != "不明") & (df["is_recent_90d"])]["instructor"].tolist())
    inactive = sorted(all_instructors - recent_instructors)
    if inactive:
        st.write("、".join(inactive[:30]))
    else:
        st.write("該当なし")

    st.subheader("受講場所マップ（東京・神奈川に寄せて表示）")
    location_counts = df["location"].value_counts()

    # Nominatimはレート制限あるので、上位だけ（cache前提）
    top_locations = location_counts.head(30)
    map_rows = []
    for location_name, cnt in top_locations.items():
        geo = geocode_location(location_name)
        # 少し間隔をあける（初回キャッシュが無いときだけ効く）
        time.sleep(0.05)
        if not geo:
            continue
        map_rows.append({"location": location_name, "count": int(cnt), "lat": geo["lat"], "lon": geo["lon"]})

    if map_rows:
        map_df = pd.DataFrame(map_rows)
        render_tokyo_area_map(map_df)
    else:
        st.info("地図化できる住所が不足しているため、場所の頻出ランキングを表示します。")
        st.bar_chart(location_counts.head(20).rename_axis("location").to_frame("count"))

    st.subheader("傾向分析コメント")
    rule_comment = build_rule_based_comment(df)
    final_comment = maybe_generate_llm_comment(rule_comment, df)
    st.write(final_comment)

    with st.expander("データプレビュー"):
        st.dataframe(df[["date", "provider", "instructor", "location", "summary", "source_url", "confidence"]])


if __name__ == "__main__":
    main()
