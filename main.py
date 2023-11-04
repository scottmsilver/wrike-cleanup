import argparse
import datetime
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import os
import json
import requests
import shutil
import subprocess

# To use # python main.py --no-do_nothing

class WrikeApi:
  def __init__(self, config_file):
    # Load the wrike API token.
    with open(config_file, 'r') as config_file:
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
      return data['data']

  def list_tasks_in_workspace(self, workspace_id):
      url = f"{self.WRIKE_BASE_URL}/folders/{workspace_id}/tasks"
      response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS)
      data = json.loads(response.text)
      return data['data']

  def list_attachments_in_task(self, task_id):
      url = f"{self.WRIKE_BASE_URL}/tasks/{task_id}/attachments"
      response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS)
      data = json.loads(response.text)
      return data['data']

  def download_attachment(self, attachment):
    url = f"{self.WRIKE_BASE_URL}/attachments/{attachment['id']}/download"
    return requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS, stream=True)

  def add_comment(self, attachment, comment_text):
    url = f"{self.WRIKE_BASE_URL}/tasks/{attachment['taskId']}/comments"
    return requests.post(url, headers=self.WRIKE_DEFAULT_HEADERS, json={"text": comment_text})

  def add_file(self, attachment, new_filename):
    url = f"{self.WRIKE_BASE_URL}/tasks/{attachment['taskId']}/attachments"
    with open(new_filename, 'rb') as f:
      files = {
        'file': (new_filename, f)
      }
      return requests.post(url, headers=self.WRIKE_DEFAULT_HEADERS, files=files)

  def delete_attachment(self, attachment):
    url = f"{self.WRIKE_BASE_URL}/attachments/{attachment['id']}"
    return requests.delete(url, headers=self.WRIKE_DEFAULT_HEADERS)

# Returns credentials by either reading from token.json or creating one from credentials.json
# Launches a webflow which on localhost.
def get_google_drive_credentials():
  creds = None
  if os.path.exists('token.json'):
    creds = Credentials.from_authorized_user_file('token.json')
  if not creds or not creds.valid:
    valid_token = False

    if creds and creds.expired and creds.refresh_token:
      try:
        creds.refresh(Request())
        valid_token = True
      except Exception as e:
        pass

    if not valid_token:
      flow = InstalledAppFlow.from_client_secrets_file(
        'credentials.json', ['https://www.googleapis.com/auth/drive.file'])
      creds = flow.run_local_server(port=0)
      with open('token.json', 'w') as token:
        token.write(creds.to_json())
  return creds
  
# Upload file filename to folder_id in Google Drive.
# Login to Google Drive using credentials.json or cause web auth if token.json is not present.
def upload_file_to_google_drive(drive_service, filename, folder_id):
  file_metadata = {'name': filename, 'parents': [folder_id]}
  uploaded_file = drive_service.files().create(body=file_metadata, media_body=MediaFileUpload(filename), fields='id').execute()
  return uploaded_file

# Create a shareable link for a given file_id.
def create_shareable_link(drive_service, file_id):
  permission = {
      'type': 'anyone',
      'role': 'reader'
  }
  drive_service.permissions().create(fileId=file_id, body=permission).execute()
  return f"https://drive.google.com/open?id={file_id}"

# The main worker for a given attachment.
# folder_id is the Google Drive folder id where to store originals.
def process_attachment(drive_service, wrike_api, attachment, folder_id):
  # 1. Download the attachment
  response = wrike_api.download_attachment(attachment)
  original_filename = attachment['name']

  try:
    with open(original_filename, 'wb') as out_file:
      shutil.copyfileobj(response.raw, out_file)    

    # 2. Upload it to Google Drive
    uploaded_file = upload_file_to_google_drive(drive_service, original_filename, folder_id)

    # 3. Create shareable link
    shareable_link = create_shareable_link(drive_service, uploaded_file['id'])

    # 4. Add the link to the file to the task
    response = wrike_api.add_comment(attachment, f"Moved attachment {attachment['name']} to Google Drive: {shareable_link}")

    # 5. If it's a photo or video, reduce quality of the original.
    file_extension = os.path.splitext(original_filename)[1]
    if file_extension in ['.jpg', '.jpeg', '.png', '.avi', '.mp4', '.heic']:
      new_filename = f"reduced_{original_filename}"
      try:
        subprocess.run(['ffmpeg', '-i', original_filename, '-vf', 'scale=iw/2:ih/2', new_filename])

        # 6. Reattach the new smaller file
        response = wrike_api.add_file(attachment, new_filename)
        print(response)
      finally:
        # Clean up the reduced file
        os.remove(new_filename)

    # 7. Delete the original attachment from the Wrike task.
    wrike_api.delete_attachment(attachment)
  finally:
    # Clean up the downloaded original file
    os.remove(original_filename)

# Get or create folder in google drive given the reference to the service.
# Returns the folder_id created.
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

# Main worker to process Wrike data.
def process_wrike(do_nothing, days_old_to_replace, default_directory, wrike_config_json):
  """Processes Wrike data.

  Args:
    do_nothing: A boolean indicating whether to do nothing.
    days_old_to_replace: The number of days old to replace.
    default_directory: The default directory.
  """
  # Initialize Google Drive API service
  creds = get_google_drive_credentials()
  drive_service = build('drive', 'v3', credentials=creds)

  # Create a the backup folder where to keep these files.
  folder_id = get_or_create_folder(drive_service, default_directory)

  wrike_api = WrikeApi(wrike_config_json)

  # Main woker to loop through all attachments.
  workspaces = wrike_api.list_workspaces()
  one_year_ago = datetime.datetime.now() - datetime.timedelta(days=days_old_to_replace)
  for workspace in workspaces:
    print(f"{workspace['title']}({workspace['id']})")
    try:
      tasks = wrike_api.list_tasks_in_workspace(workspace['id'])
      for task in tasks:
        try:
          attachments = wrike_api.list_attachments_in_task(task['id'])
          for attachment in attachments:
            created_date = datetime.datetime.strptime(attachment['createdDate'], "%Y-%m-%dT%H:%M:%SZ")

            # All already reduces files are presumed to have "reduced" in the name
            if created_date < one_year_ago and 'reduced' not in attachment['name']:
              print(attachment['name'])
              print(attachment)
              try:
                if not do_nothing:
                  process_attachment(drive_service, wrike_api, attachment, folder_id)
                else:
                  print("Do nothing.")
              except Exception as e:
                print(e) 
        except Exception as e:
          print(f'couldnt list attachments in task: {task} because of {e}')
    except Exception as e:
      print(f'couldnt list tasks in workspace: {workspace} because of {e}')

if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument('--do_nothing', action=argparse.BooleanOptionalAction)
  parser.add_argument("--days_old_to_replace", type=int, default=365, help="The number of days old to replace.")
  parser.add_argument("--default_directory", type=str, default="Wrike Backup", help="The default directory.")
  parser.add_argument("--wrike_config_json", type=str, default="config.json", help="The default path to the config json for Wrike.")
  args = parser.parse_args()

  process_wrike(args.do_nothing, args.days_old_to_replace, args.default_directory, args.wrike_config_json)

