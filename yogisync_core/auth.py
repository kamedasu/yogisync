from __future__ import annotations

from typing import List

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


def get_credentials(scopes: List[str], client_secret_path: str, token_path: str) -> Credentials:
    creds = None
    try:
        creds = Credentials.from_authorized_user_file(token_path, scopes=scopes)
    except Exception:
        creds = None

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    elif not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, scopes=scopes)
        creds = flow.run_local_server(port=0)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds
