"""Wrike v4 REST API client.

Shared by both tools in this repo (the root-level cleanup CLI and the
preview Cloud Run service). Bearer-token auth via `config_file`. No retry,
no rate-limit handling, no pagination handled centrally — methods that
paginate (e.g. `list_account_attachments`) expose the page token directly
to the caller.
"""

import json
from pathlib import Path

import requests


class WrikeApi:
    """Thin REST wrapper around Wrike's v4 API.

    Methods either return a parsed `data` list/dict from the JSON envelope,
    or the raw `requests.Response` for stream/upload calls. Callers must
    handle non-2xx outcomes themselves where the method doesn't call
    `raise_for_status` (most legacy methods don't; newer methods do).
    """

    def __init__(self, config_file):
        """Read `{"WRIKE_API_TOKEN": "..."}` from `config_file` and build
        the default Authorization header."""
        with open(config_file, "r") as config_file:
            config = json.load(config_file)

        self.WRIKE_API_TOKEN = config["WRIKE_API_TOKEN"]
        self.WRIKE_BASE_URL = "https://www.wrike.com/api/v4"
        self.WRIKE_DEFAULT_HEADERS = {
            "Authorization": f"Bearer {self.WRIKE_API_TOKEN}",
        }

    # ----- Legacy methods (used by root-level main.py cleanup CLI) -----

    def list_workspaces(self):
        """Return all top-level folders (Wrike calls them workspaces).

        Has a `print(data)` side effect from the original implementation;
        kept for backward compatibility with `main.py`.
        """
        url = f"{self.WRIKE_BASE_URL}/folders"
        response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS)
        data = json.loads(response.text)
        print(data)
        return data["data"]

    def list_tasks_in_workspace(self, workspace_id):
        """List tasks under a folder/workspace by API ID."""
        url = f"{self.WRIKE_BASE_URL}/folders/{workspace_id}/tasks"
        response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS)
        data = json.loads(response.text)
        return data["data"]

    def list_attachments_in_task(self, task_id):
        """List attachments on a task by API ID."""
        url = f"{self.WRIKE_BASE_URL}/tasks/{task_id}/attachments"
        response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS)
        data = json.loads(response.text)
        return data["data"]

    def download_attachment(self, attachment):
        """Stream-download an attachment. Takes the full attachment dict
        (uses `attachment['id']`). Returns the raw streaming Response."""
        url = f"{self.WRIKE_BASE_URL}/attachments/{attachment['id']}/download"
        return requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS, stream=True)

    def add_comment(self, attachment, comment_text):
        """Post a comment to the task the attachment lives on."""
        url = f"{self.WRIKE_BASE_URL}/tasks/{attachment['taskId']}/comments"
        return requests.post(url, headers=self.WRIKE_DEFAULT_HEADERS, json={"text": comment_text})

    def add_file(self, attachment, new_filename):
        """Upload a local file to the task the given attachment is on.

        Used by `main.py`'s "reduced" image path. New code should prefer
        `add_file_to_task(task_id, file_path, upload_name=)` below.
        """
        url = f"{self.WRIKE_BASE_URL}/tasks/{attachment['taskId']}/attachments"
        with open(new_filename, "rb") as f:
            files = {"file": (new_filename, f)}
            return requests.post(url, headers=self.WRIKE_DEFAULT_HEADERS, files=files)

    def delete_attachment(self, attachment):
        """Delete an attachment by full attachment dict."""
        url = f"{self.WRIKE_BASE_URL}/attachments/{attachment['id']}"
        return requests.delete(url, headers=self.WRIKE_DEFAULT_HEADERS)

    # ----- Methods added for the preview service -----

    def get_attachment(self, attachment_id):
        """Return the attachment metadata dict for `attachment_id`, or
        `None` if Wrike returns 404 (attachment deleted)."""
        url = f"{self.WRIKE_BASE_URL}/attachments/{attachment_id}"
        response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()["data"][0]

    def download_attachment_by_id(self, attachment_id):
        """Stream-download by attachment ID (no need to pass the full dict).
        Returns the raw streaming Response — caller checks status_code."""
        url = f"{self.WRIKE_BASE_URL}/attachments/{attachment_id}/download"
        return requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS, stream=True)

    def add_file_to_task(self, task_id, file_path, upload_name=None):
        """Upload a local file to `task_id` with an optional rename.

        Coerces `file_path` to `Path` so str/Path inputs both work.
        Returns the new attachment's API id (str). Raises on non-2xx.
        """
        file_path = Path(file_path)
        upload_name = upload_name or file_path.name
        url = f"{self.WRIKE_BASE_URL}/tasks/{task_id}/attachments"
        with open(file_path, "rb") as fh:
            files = {"file": (upload_name, fh)}
            response = requests.post(url, headers=self.WRIKE_DEFAULT_HEADERS, files=files)
        response.raise_for_status()
        return response.json()["data"][0]["id"]

    def list_account_attachments(self, created_from, created_to, next_page_token=None):
        """Paginated list of account-scoped attachments in a date range.

        `created_from` and `created_to` are RFC3339 strings (e.g.
        `2026-05-01T00:00:00Z`). Wrike's `createdDate` filter is documented
        to require windows smaller than 31 days; callers (e.g. `reconcile`)
        chunk accordingly.

        Returns `(items, next_page_token)`. When `next_page_token` is None
        in the response, pagination is done.
        """
        url = f"{self.WRIKE_BASE_URL}/attachments"
        params = {
            "createdDate": '{"start":"' + created_from + '","end":"' + created_to + '"}',
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token
        response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS, params=params)
        response.raise_for_status()
        body = response.json()
        return body["data"], body.get("responseNextPageToken")

    def delete_attachment_by_id(self, attachment_id):
        """Delete by attachment ID. Returns the raw Response — caller checks status."""
        url = f"{self.WRIKE_BASE_URL}/attachments/{attachment_id}"
        return requests.delete(url, headers=self.WRIKE_DEFAULT_HEADERS)
