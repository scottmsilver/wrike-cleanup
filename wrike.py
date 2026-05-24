import json

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
