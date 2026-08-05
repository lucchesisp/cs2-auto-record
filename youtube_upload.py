import argparse
import os
import sys
import time

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRET = os.path.join(BASE_DIR, "client_secret.json")
TOKEN_FILE = os.path.join(BASE_DIR, "token.json")
LOG_FILE = os.path.join(BASE_DIR, "youtube_upload.log")


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def get_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not os.path.exists(CLIENT_SECRET):
            log(f"missing {CLIENT_SECRET}, download it from Google Cloud Console")
            sys.exit(1)
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
        creds = flow.run_local_server(port=0)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    return creds


def upload(file_path, title, privacy, description):
    youtube = build("youtube", "v3", credentials=get_credentials())
    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "categoryId": "20",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(file_path, chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body,
                                      media_body=media)
    log(f"uploading: {file_path} as \"{title}\" ({privacy})")
    response = None
    errors = 0
    last_pct = -1
    while response is None:
        try:
            status, response = request.next_chunk()
            errors = 0
            if status:
                pct = int(status.progress() * 100)
                if pct >= last_pct + 10:
                    last_pct = pct
                    log(f"progress: {pct}%")
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504):
                errors += 1
                if errors > 10:
                    raise
                wait = min(2 ** errors, 60)
                log(f"HTTP {e.resp.status}, retrying in {wait}s")
                time.sleep(wait)
            else:
                raise
    log(f"done: https://youtu.be/{response['id']}")
    return response["id"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", nargs="?")
    parser.add_argument("--title")
    parser.add_argument("--privacy", default="private",
                        choices=["private", "unlisted", "public"])
    parser.add_argument("--description", default="")
    parser.add_argument("--auth-only", action="store_true")
    args = parser.parse_args()

    if args.auth_only:
        get_credentials()
        log("authorized, token saved")
        return
    if not args.file or not os.path.exists(args.file):
        log(f"file not found: {args.file}")
        sys.exit(1)

    title = args.title or os.path.splitext(os.path.basename(args.file))[0]
    try:
        upload(args.file, title, args.privacy, args.description)
    except Exception as e:
        log(f"upload failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
