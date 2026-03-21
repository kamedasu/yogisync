from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# --- Optional dependencies (requirements に入ってる想定) ---
import plotly.express as px
from google.oauth2 import service_account
from googleapiclient.discovery import build


# ============================================================
# Config / Secrets helpers
# ============================================================

def _get_secret(key: str) -> Any:
    # Streamlit Cloud: st.secrets
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return None


def _get_calendar_id() -> str:
    # Prefer env, then secrets
    cid = os.getenv("YOGISYNC_CALENDAR_ID") or _get_secret("YOGISYNC_CALENDAR_ID")
    if not cid:
        raise RuntimeError("YOGISYNC_CALENDAR_ID is not configured.")
    return str(cid).strip()


def _get_service_account_info() -> Dict[str, Any]:
    """
    A案: Streamlit Cloud の secrets.toml に gcp_service_account を置く
      [gcp_service_account]
      type="service_account"
      project_id="..."
      private_key_id="..."
      private_key="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
      client_email="..."
      ...
    もしくはローカル用に GOOGLE_SERVICE_ACCOUNT_PATH でJSONパス指定
    """
    sa = _get_secret("gcp_service_account")
    if sa:
        # st.secrets は dict 相当で来る
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
# Google Calendar fetch
# ============================================================

@st.cache_data(ttl=300, show_spinner=False)
def fetch_events(calendar_id: str) -> list[dict[str, Any]]:
    """
    直近〜未来の一定期間をまとめて取得（必要なら期間は調整）
    """
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
    """
    description に入っている想定:
      provider: xxx
      event_uid: ...
      reservation_id: ...
      source_url: ...
      confidence: ...
      address: ...
      instructor: ...
      base_price: ...
    """
    meta: dict[str, str] = {}
    if not desc:
        return meta

    for line in desc.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower()
        v = v.strip()
        if not k:
            continue
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
        # all-day: date only -> treat as noon JST-ish (but keep UTC)
        s = start["date"]
        try:
            d = datetime.fromisoformat(s).date()
            return datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def extract_instructor(summary: str, meta: dict[str, str]) -> str:
    if meta.get("instructor"):
        return meta["instructor"].strip() or "不明"

    # summary like: "[PEATIX] xxx - instructor"
    if " - " in summary:
        tail = summary.rsplit(" - ", 1)[-1].strip()
        if tail and len(tail) <= 40:
            return tail

    m = re.search(r"(?:講師|Instructor)[:：]\s*([^\n\r]+)", summary, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()

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
    """
    目標:
      - 東京都 => 区/市 を拾って "東京:渋谷区" のように返す
      - 神奈川県 => 市/区 を拾って "神奈川:横浜市" or "神奈川:横浜市中区" 的に返す
      - それ以外/不明 => "その他" 扱い
    """
    if not location:
        return "その他"

    s = location.strip()

    # 東京都: 〜区 / 〜市
    if "東京都" in s:
        m = re.search(r"東京都.*?([^\s　]+?区)", s)
        if m:
            return f"東京:{m.group(1)}"
        m = re.search(r"東京都.*?([^\s　]+?市)", s)
        if m:
            return f"東京:{m.group(1)}"
        # 住所っぽくない場合はヒントで補完
        for k, v in _TOKYO_HINTS.items():
            if k in s:
                return v
        return "東京:不明"

    # 神奈川県: 市 (横浜市中区みたいに市+区も拾う)
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

    # 県名が書かれてないケース（場所名だけ）を軽く拾う
    for k, v in _TOKYO_HINTS.items():
        if k in s:
            return v

    # それ以外
    return "その他"


def make_area_pie(df: pd.DataFrame) -> pd.DataFrame:
    # location を area に変換
    areas = df["location"].fillna("").astype(str).map(extract_area)
    counts = areas.value_counts()

    # 「東京:*」「神奈川:*」を中心に見せたい（その他はまとめ）
    # まず上位を取って、残りはその他へ
    top_n = 12
    top = counts.head(top_n)
    others = counts.iloc[top_n:].sum()
    if others > 0:
        top = pd.concat([top, pd.Series({"その他(まとめ)": others})])

    pie_df = top.rename_axis("area").reset_index(name="count")
    pie_df["pct"] = (pie_df["count"] / pie_df["count"].sum() * 100).round(1)
    return pie_df


# ============================================================
# Rule-based comment + Optional LLM comment
# ============================================================

def build_rule_based_comment(df: pd.DataFrame) -> str:
    lines: list[str] = []

    total = len(df)
    recent_90 = int(df["is_recent_90d"].sum())
    this_month = int(df["is_this_month"].sum())
    lines.append(f"過去データ全体で {total} 件、直近90日で {recent_90} 件、当月で {this_month} 件の受講があります。")

    top_provider = df["provider"].value_counts().head(1)
    if not top_provider.empty:
        lines.append(f"最も多いproviderは {top_provider.index[0]}（{int(top_provider.iloc[0])}件）です。")

    top_instructor = df[df["instructor"] != "不明"]["instructor"].value_counts().head(1)
    if not top_instructor.empty:
        lines.append(f"最も受講が多い講師は {top_instructor.index[0]}（{int(top_instructor.iloc[0])}件）です。")

    intensity = df["intensity"].value_counts(normalize=True).round(3).to_dict()
    hard_ratio = float(intensity.get("ハード寄り", 0.0))
    soft_ratio = float(intensity.get("リラックス寄り", 0.0))
    if hard_ratio >= 0.45:
        lines.append("全体としてハード寄りのクラス比率が高めです。")
    elif soft_ratio >= 0.45:
        lines.append("全体としてリラックス寄りのクラス比率が高めです。")
    else:
        lines.append("強度傾向はバランス型です。")

    # 地域のざっくり傾向
    area_counts = df["location"].fillna("").astype(str).map(extract_area).value_counts()
    if not area_counts.empty:
        top_area = area_counts.index[0]
        lines.append(f"受講場所は「{top_area}」が最多です。")

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
        text = (resp.output_text or "").strip()
        return text or rule_comment
    except Exception:
        return rule_comment


# ============================================================
# Build dataframe
# ============================================================

def build_dataframe(events: list[dict[str, Any]]) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    rows = []

    for item in events:
        dt = extract_datetime(item)
        if dt is None:
            continue

        summary = item.get("summary", "") or ""
        desc = item.get("description", "") or ""
        meta = parse_description(desc)

        provider = meta.get("provider") or "unknown"
        location = item.get("location") or meta.get("address") or meta.get("location_name") or "未設定"
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
                "instructor",
                "location",
                "area",
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
    df["area"] = df["area"].replace("", "その他").fillna("その他")
    return df.sort_values("date", ascending=False)


# ============================================================
# UI
# ============================================================

def main() -> None:
    load_dotenv()

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

    # KPIs
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

    # ✅ Map removed, replaced with area pie chart
    st.subheader("受講エリア比率（東京都=区別 / 神奈川=市・区別）")
    pie_df = make_area_pie(df)

    fig = px.pie(
        pie_df,
        names="area",
        values="count",
        hover_data=["pct"],
        labels={"pct": "%"},
    )
    fig.update_traces(textposition="inside", textinfo="percent+label")
    st.plotly_chart(fig, use_container_width=True)

    # 補助：ランキングも出す
    with st.expander("エリア内訳（件数）"):
        area_counts = (
            df["area"].value_counts().rename_axis("area").to_frame("count").reset_index()
        )
        st.dataframe(area_counts, use_container_width=True)

    st.subheader("傾向分析コメント")
    rule_comment = build_rule_based_comment(df)
    final_comment = maybe_generate_llm_comment(rule_comment, df)
    st.write(final_comment)

    with st.expander("データプレビュー"):
        st.dataframe(
            df[["date", "provider", "instructor", "area", "location", "summary", "source_url", "confidence"]],
            use_container_width=True,
        )


if __name__ == "__main__":
    main()
