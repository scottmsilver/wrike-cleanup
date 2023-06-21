import requests
import json
import os
import requests
import datetime
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import subprocess
import shutil

# Set your Wrike API token here
with open('config.json', 'r') as config_file:
    config = json.load(config_file)

API_TOKEN = config["WRIKE_API_TOKEN"]

# Base URL for all Wrike API calls
BASE_URL = "https://www.wrike.com/api/v4"

# Set up headers for the API calls
headers = {
    "Authorization": f"Bearer {API_TOKEN}",
}

def list_workspaces():
    url = f"{BASE_URL}/folders"
    response = requests.get(url, headers=headers)
    data = json.loads(response.text)
    return data['data']

def list_tasks_in_workspace(workspace_id):
    url = f"{BASE_URL}/folders/{workspace_id}/tasks"
    response = requests.get(url, headers=headers)
    data = json.loads(response.text)
    return data['data']

def list_attachments_in_task(task_id):
    url = f"{BASE_URL}/tasks/{task_id}/attachments"
    response = requests.get(url, headers=headers)
    data = json.loads(response.text)
    return data['data']

# Get one of these from cloud console with a project set up to be able to access
# the Google Drive API
def get_google_drive_credentials():
  creds = None
  if os.path.exists('token.json'):
      creds = Credentials.from_authorized_user_file('token.json')
  if not creds or not creds.valid:
      if creds and creds.expired and creds.refresh_token:
          creds.refresh(Request())
      else:
          flow = InstalledAppFlow.from_client_secrets_file(
              'credentials.json', ['https://www.googleapis.com/auth/drive.file'])
          creds = flow.run_local_server(port=0)
      with open('token.json', 'w') as token:
          token.write(creds.to_json())
  return creds

def process_attachments(attachment, folder_id):
    # 1. Download the attachment
    url = f"{BASE_URL}/attachments/{attachment['id']}/download"
    response = requests.get(url, headers=headers, stream=True)
    original_filename = attachment['name']
    with open(original_filename, 'wb') as out_file:
        shutil.copyfileobj(response.raw, out_file)    

    # 2. Upload it to Google Drive
    # We assume here you have 'credentials.json' file with Google Drive API credentials
    creds = get_google_drive_credentials()

    drive_service = build('drive', 'v3', credentials=creds)
    file_metadata = {'name': original_filename, 'parents': [folder_id]}
    media = MediaFileUpload(original_filename, mimetype='*/*')
    uploaded_file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()

    # 3. Create shareable link
    permission = {
        'type': 'anyone',
        'role': 'reader'
    }
    drive_service.permissions().create(fileId=uploaded_file['id'], body=permission).execute()
    shareable_link = f"https://drive.google.com/open?id={uploaded_file['id']}"

    # 4. Add that link to the task
    url = f"{BASE_URL}/tasks/{attachment['taskId']}/comments"
    comment_text = f"Moved attachment {attachment['name']} to Google Drive: {shareable_link}"
    response = requests.post(url, headers=headers, json={"text": comment_text})

    # 5. If it's a photo or video, reduce quality
    file_extension = os.path.splitext(original_filename)[1]
    if file_extension in ['.jpg', '.jpeg', '.png', '.avi', '.mp4']:
        new_filename = f"reduced_{original_filename}"
        subprocess.run(['ffmpeg', '-i', original_filename, '-vf', 'scale=iw/2:ih/2', new_filename])

        # 6. Reattach the new smaller file
        url = f"{BASE_URL}/tasks/{attachment['taskId']}/attachments"
        with open(new_filename, 'rb') as f:
            files = {
                'file': (new_filename, f)
            }
            response = requests.post(url, headers=headers, files=files)
            print(response)

        # Clean up the reduced file
        os.remove(new_filename)

    # 7. Delete the original attachment
    url = f"{BASE_URL}/attachments/{attachment['id']}"
    response = requests.delete(url, headers=headers)

    # Clean up the downloaded original file
    os.remove(original_filename)


def get_or_create_folder(service, folder_name):
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder'"
    response = service.files().list(q=query).execute()
    folder = response.get('files', [])

    if not folder:
        # If the folder does not exist, create it
        metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        folder = service.files().create(body=metadata, fields='id').execute()
    
    folder_id = folder[0]['id'] if isinstance(folder, list) else folder['id']
    
    return folder_id

# Initialize Google Drive API service
creds = get_google_drive_credentials()
service = build('drive', 'v3', credentials=creds)

# Find the folder or create it if it doesn't exist
folder_name = 'Wrike Backup'
folder_id = get_or_create_folder(service, folder_name)

# Main code
workspaces = list_workspaces()
one_year_ago = datetime.datetime.now() - datetime.timedelta(days=365)
for workspace in workspaces:
    print(f"{workspace['title']}({workspace['id']})")
    tasks = list_tasks_in_workspace(workspace['id'])
    for task in tasks:
        attachments = list_attachments_in_task(task['id'])
        for attachment in attachments:
            created_date = datetime.datetime.strptime(attachment['createdDate'], "%Y-%m-%dT%H:%M:%SZ")
            if created_date < one_year_ago and 'reduced' not in attachment['name']:
                print(attachment['name'])
                print(attachment)
                try:
                  process_attachments(attachment, folder_id)
                except Exception as e:
                  print(e) 
