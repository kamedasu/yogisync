# YogiSync

YogiSync は、以下の2フェーズ構成の個人向けアプリです。
- Phase1: Gmail予約メールを解析して YogiSync 用 Google Calendar に同期（ローカル実行）
- Phase2: Google Calendar を読み取り、Streamlit で活動分析を可視化

## ディレクトリ構成

```text
yogisync/
├── streamlit_app.py
├── yogisync_core/
├── secrets/                  # ローカル専用（gitignore）
│   ├── client_secret.json
│   └── token.json
├── data/
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## 1) Phase1: ローカル同期エンジン（OAuth Installed App）

### Google Cloud 準備
1. Google Cloud Console でプロジェクト作成
2. Gmail API / Google Calendar API を有効化
3. OAuth 同意画面を設定（テストユーザーに自分のGmailを追加）
4. OAuthクライアント（デスクトップアプリ）を作成
5. `client_secret.json` を `secrets/client_secret.json` に配置

### セットアップ
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`.env` を作成（`.env.example` をコピーして編集）:
```env
GMAIL_QUERY=newer_than:365d
GOOGLE_CLIENT_SECRET_PATH=secrets/client_secret.json
GOOGLE_TOKEN_PATH=secrets/token.json
YOGISYNC_CALENDAR_ID=YOUR_CALENDAR_ID
TIMEZONE=Asia/Tokyo
DEFAULT_EVENT_DURATION_MINUTES=60
SQLITE_PATH=data/yogisync.db
```

### 実行
```bash
python -m yogisync_core.cli sync --limit 50
```

## 2) Phase2: Streamlit 分析ダッシュボード（サービスアカウント読み取り）

### 方針
- Streamlit 側は `token.json` を使わず、**サービスアカウント**で Google Calendar を読み取り
- 必要スコープ: `https://www.googleapis.com/auth/calendar.readonly`
- 読み取り対象カレンダーは `YOGISYNC_CALENDAR_ID`

### Streamlit Cloud Secrets 設定例
Streamlit Cloud の `Secrets` に以下を設定:

```toml
YOGISYNC_CALENDAR_ID = "your_calendar_id@group.calendar.google.com"
OPENAI_API_KEY = "sk-..." # 任意（未設定でも動作）
OPENAI_MODEL = "gpt-5-mini" # 任意

[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "...@....iam.gserviceaccount.com"
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
universe_domain = "googleapis.com"
```

### サービスアカウントへのカレンダー共有
1. Google Calendar の YogiSync 専用カレンダーを開く
2. 「設定と共有」→「特定のユーザーとの共有」へ進む
3. サービスアカウントの `client_email` を追加
4. 権限を「予定の表示（閲覧）」以上に設定

## 3) ローカルで Streamlit を実行

### 3-1. サービスアカウントJSONをローカルファイルで使う場合
- `secrets/service_account.json` を配置（gitignore対象）
- 必要に応じて `.env` に `GOOGLE_SERVICE_ACCOUNT_PATH=secrets/service_account.json` を設定

### 3-2. 実行
```bash
streamlit run streamlit_app.py
```

## 4) 動作確認

```bash
python -m compileall yogisync_core
streamlit run streamlit_app.py
```

## 補足
- Phase2 のAIコメントは、`OPENAI_API_KEY` が未設定でもルールベース分析で必ず動作します。
- 既存の `yogisync_core` ロジックは維持し、設定パスのデフォルトのみ `secrets/` に合わせています。
