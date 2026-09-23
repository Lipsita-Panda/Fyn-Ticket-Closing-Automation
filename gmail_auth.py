"""
Shared Gmail API authentication helper.

First call: opens a browser, you log in as the mailbox you want this to
read (deployments@fynmobility.com or lipsita.panda@fynmobility.com,
whichever actually receives the ticket-closing emails) and approve access.
Saves a token.json file so future runs reuse it without opening a browser
again, until it expires or is revoked.

Needs credentials.json (downloaded from Google Cloud Console) in the same
folder.
"""
import os.path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",  # read + mark as read/labels
    "https://www.googleapis.com/auth/gmail.send",     # send the "Done" reply
]

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"


def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)
