from __future__ import annotations

import io
import re
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"


def safe_filename_part(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:80] or "unknown"


class DriveService:
    def __init__(self, service_account_info: dict[str, Any], parent_folder_id: str) -> None:
        credentials = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self.parent_folder_id = parent_folder_id
        self._folder_ids: dict[tuple[str, str], str] = {}

    def _get_or_create_folder(self, name: str, parent_id: str) -> str:
        cache_key = (parent_id, name)
        if cache_key in self._folder_ids:
            return self._folder_ids[cache_key]

        escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
        response = (
            self._service.files()
            .list(
                q=(
                    f"name = '{escaped_name}' and mimeType = '{FOLDER_MIME}' "
                    f"and '{parent_id}' in parents and trashed = false"
                ),
                fields="files(id,name)",
                pageSize=100,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        folders = response.get("files", [])
        if folders:
            folder_id = folders[0]["id"]
        else:
            created = (
                self._service.files()
                .create(
                    body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
            folder_id = created["id"]
        self._folder_ids[cache_key] = folder_id
        return folder_id

    def upload_bill(
        self,
        content: bytes,
        name: str,
        month: str,
        filename: str,
        mime_type: str,
        flow_type: str = "Reimbursement",
    ) -> str:
        flow_folder = {
            "Direct Payment Flow": "Direct Payments",
            "Report": "Reports",
        }.get(flow_type, "Reimbursements")
        flow_folder_id = self._get_or_create_folder(flow_folder, self.parent_folder_id)
        month_id = self._get_or_create_folder(month, flow_folder_id)
        user_id = self._get_or_create_folder(safe_filename_part(name), month_id)
        uploaded = (
            self._service.files()
            .create(
                body={"name": filename, "parents": [user_id]},
                media_body=MediaIoBaseUpload(
                    io.BytesIO(content), mimetype=mime_type, resumable=False
                ),
                fields="id,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        return uploaded.get("webViewLink") or f"https://drive.google.com/open?id={uploaded['id']}"
