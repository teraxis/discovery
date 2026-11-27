"""Тека Google Drive як джерело (необов'язково): рекурсивний обхід через Drive API.

Підключення один раз: покладіть свій OAuth-клієнт (тип «Desktop app») у
~/.nlm-pipeline/client_secret.json і виконайте `nlm drive-auth`. Інструкція — AGENT_SETUP.md.
"""
from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

HOME = Path(os.environ.get("NLM_HOME") or Path.home() / ".nlm-pipeline").expanduser()
CLIENT = HOME / "client_secret.json"
TOKEN = HOME / "drive_token.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FOLDER = "application/vnd.google-apps.folder"
# Типи, які NotebookLM бере з Drive за посиланням (без завантаження)
BY_REF = {"application/vnd.google-apps.document": "google-doc",
          "application/vnd.google-apps.presentation": "google-slides",
          "application/vnd.google-apps.spreadsheet": "google-sheets",
          "application/pdf": "pdf"}


def folder_id(spec: str) -> str:
    m = re.search(r"/folders/([\w-]{10,})", spec) or re.search(r"[?&]id=([\w-]{10,})", spec)
    return m.group(1) if m else spec.strip()


def is_drive(spec: str) -> bool:
    return "drive.google.com" in spec or bool(re.fullmatch(r"[\w-]{25,}", spec.strip()))


def auth() -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not CLIENT.exists():
        print(f"✗ Немає {CLIENT}. Створіть OAuth-клієнт «Desktop app» у Google Cloud (див. AGENT_SETUP.md).")
        return 2
    creds = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES).run_local_server(port=0)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    print(f"✓ Google Drive підключено (лише читання). Токен: {TOKEN}")
    return 0


def service(account: str | None = None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    if not TOKEN.exists():
        raise RuntimeError("Google Drive API не підключено. Людина має виконати: nlm drive-auth "
                           "(або вкажіть локальну теку Drive for Desktop замість посилання).")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception as e:
            raise RuntimeError("Вхід Google Drive прострочено. Виконайте: nlm drive-auth") from e
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def walk(svc, fid: str, path: str = ""):
    """→ dict(id, name, mimeType, size, path) для всіх файлів дерева."""
    stack = [(fid, path)]
    while stack:
        cur, p = stack.pop()
        tok = None
        while True:
            r = svc.files().list(q=f"'{cur}' in parents and trashed = false", pageSize=1000, pageToken=tok,
                                 fields="nextPageToken, files(id,name,mimeType,size)").execute()
            for f in r.get("files", []):
                fp = f"{p}/{f['name']}" if p else f["name"]
                if f["mimeType"] == FOLDER:
                    stack.append((f["id"], fp))
                elif not f["mimeType"].startswith("application/vnd.google-apps.shortcut"):
                    yield {**f, "path": fp}
            tok = r.get("nextPageToken")
            if not tok:
                break


def download(svc, f: dict, cache: Path) -> Path:
    from googleapiclient.http import MediaIoBaseDownload
    out = cache / f["id"] / f["name"]
    if out.exists() and (not f.get("size") or out.stat().st_size == int(f["size"])):
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    buf = io.FileIO(str(out), "wb")
    dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=f["id"]), chunksize=8 * 1024 * 1024)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.close()
    return out
