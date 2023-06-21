# wrike-cleanup

Wrike Cleanup reduces your storage usage on Wrike by doing the following for attachments older than 1 year:

- Moving it Google Drive and adding a new comment on the task with a link. 
- Attaching a smaller version of the original, if it's an image or video.
- Deleting it.

# Installation

```pip3 install -r requirements.txt```

# Configuration

You'll need to configure two files

## credentials.json

Download a credentials.json from a new Google Cloud project with access to the Google Drive API and put it in the directory
from where you will run wrike-cleanup.

## config.json

Take the original and get your Wrike API key and put it in there. NB: wrike-cleanup will read it from the current working directory.

# Usage

```
python main.py
```

When you first use it it will prompt you to login to Google and give it access to Google Drive.
This will create a token.json file which caching credentials for subsequent runs.


