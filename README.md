# YogiSync (Phase1)

Gmailの予約メールからイベント情報を抽出し、YogiSync専用Google Calendarへ同期するローカル同期エンジンです。
Cloud側（Streamlit Community Cloud予定）はカレンダーを読み取り専用で表示する構成を想定しています。

## 1) Google Cloud準備（概要）
1. Google Cloud Consoleで新規プロジェクトを作成
2. Gmail API と Google Calendar API を有効化
3. OAuth同意画面を設定（テストユーザーに自分のGmailを追加）
4. OAuthクライアント（デスクトップアプリ）を作成
5. `client_secret.json` を本リポジトリ直下に配置

## 2) セットアップ
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`.env` を作成（例： `.env.example` をコピーして編集）:
```
GMAIL_QUERY=newer_than:365d
GOOGLE_CLIENT_SECRET_PATH=client_secret.json
GOOGLE_TOKEN_PATH=token.json
YOGISYNC_CALENDAR_ID=YOUR_CALENDAR_ID
TIMEZONE=Asia/Tokyo
SQLITE_PATH=data/yogisync.db
```

## 3) 実行
```bash
python -m yogisync_core.cli sync --limit 50
```

## 4) 設計メモ
- Gmail → provider判定 → provider別パーサ → event_uidで重複排除 → Google Calendarへupsert
- SQLiteに同期状態（event_uid / gcal_event_id / content_hash）を保存
- Cloud側は **YogiSync専用カレンダーの読み取りのみ** を想定

## 5) ディレクトリ構成
```
yogisync_core/
  config.py
  models.py
  auth.py
  collector_gmail.py
  provider_detect.py
  parsers/
  store.py
  sync_gcal.py
  pipeline.py
  cli.py

data/
.env.example
requirements.txt
README.md
```
