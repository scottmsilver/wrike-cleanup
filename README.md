# wrike-cleanup

Wrike Cleanup reduces your storage usage on Wrike by doing the following for attachments older than 1 year:

- Moving it Google Drive and adding a new comment on the task with a link. (NB: permissions on backup file are wide open)
- Attaching a smaller version of the original, if it's an image or video.
- Deleting it.

# Installation

```pip3 install -r requirements.txt```

# Configuration

You'll need to configure two files

## credentials.json

Download a credentials.json from a new Google Cloud project with access to the Google Drive API and put it in the directory
from where you will run wrike-cleanup (currenty working directory)

## config.json

Take the original config.json and get your Wrike API key and put it in there. 
NB: wrike-cleanup will read it from the current working directory by default.

# Usage

This will show you what it would do. When you first use it it will prompt you to login to Google and give it access to Google Drive.
This will create a token.json file which caches credentials for subsequent runs.
NB: the working directory must be writable.

```
python main.py
```

This will actually run it with defaults, but will do something.

```
python main.py --no-do_nothing
```
Here are some other commands line arguments:
```
--do_nothing: Don't actually do anything (opposite --no-do_nothing)
--days_old_to_replace: The number of days old to replace.
--originals_directory: The Google Drive directory where to store originals
--wrike_config_json: Where to find the config.json file with the Wrike API key  
--wrike_api_rate_limit: Limits for calling wrike api in calls per minute
```




