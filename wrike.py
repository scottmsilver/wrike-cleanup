import json
from pathlib import Path

import requests


class WrikeApi:
    def __init__(self, config_file):
        # Load the wrike API token.
        with open(config_file, "r") as config_file:
            config = json.load(config_file)

        self.WRIKE_API_TOKEN = config["WRIKE_API_TOKEN"]

        # Base URL for all Wrike API calls
        self.WRIKE_BASE_URL = "https://www.wrike.com/api/v4"

        # Set up headers for the API calls
        self.WRIKE_DEFAULT_HEADERS = {
            "Authorization": f"Bearer {self.WRIKE_API_TOKEN}",
        }

    def list_workspaces(self):
        url = f"{self.WRIKE_BASE_URL}/folders"
        response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS)
        data = json.loads(response.text)
        print(data)
        return data["data"]

    def list_tasks_in_workspace(self, workspace_id):
        url = f"{self.WRIKE_BASE_URL}/folders/{workspace_id}/tasks"
        response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS)
        data = json.loads(response.text)
        return data["data"]

    def list_attachments_in_task(self, task_id):
        url = f"{self.WRIKE_BASE_URL}/tasks/{task_id}/attachments"
        response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS)
        data = json.loads(response.text)
        return data["data"]

    def download_attachment(self, attachment):
        url = f"{self.WRIKE_BASE_URL}/attachments/{attachment['id']}/download"
        return requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS, stream=True)

    def add_comment(self, attachment, comment_text):
        url = f"{self.WRIKE_BASE_URL}/tasks/{attachment['taskId']}/comments"
        return requests.post(url, headers=self.WRIKE_DEFAULT_HEADERS, json={"text": comment_text})

    def add_file(self, attachment, new_filename):
        url = f"{self.WRIKE_BASE_URL}/tasks/{attachment['taskId']}/attachments"
        with open(new_filename, "rb") as f:
            files = {"file": (new_filename, f)}
            return requests.post(url, headers=self.WRIKE_DEFAULT_HEADERS, files=files)

    def delete_attachment(self, attachment):
        url = f"{self.WRIKE_BASE_URL}/attachments/{attachment['id']}"
        return requests.delete(url, headers=self.WRIKE_DEFAULT_HEADERS)

    def get_attachment(self, attachment_id):
        """Return the attachment metadata, or None on 404."""
        url = f"{self.WRIKE_BASE_URL}/attachments/{attachment_id}"
        response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()["data"][0]

    def download_attachment_by_id(self, attachment_id):
        url = f"{self.WRIKE_BASE_URL}/attachments/{attachment_id}/download"
        return requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS, stream=True)

    def add_file_to_task(self, task_id, file_path, upload_name=None):
        """Upload a file to a task with an optional override filename. Returns the new
        attachment's id."""
        file_path = Path(file_path)
        upload_name = upload_name or file_path.name
        url = f"{self.WRIKE_BASE_URL}/tasks/{task_id}/attachments"
        with open(file_path, "rb") as fh:
            files = {"file": (upload_name, fh)}
            response = requests.post(url, headers=self.WRIKE_DEFAULT_HEADERS, files=files)
        response.raise_for_status()
        return response.json()["data"][0]["id"]

    def list_account_attachments(self, created_from, created_to, next_page_token=None):
        """List attachments in the account in a date range. Returns (items, nextPageToken)."""
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
        url = f"{self.WRIKE_BASE_URL}/attachments/{attachment_id}"
        return requests.delete(url, headers=self.WRIKE_DEFAULT_HEADERS)
