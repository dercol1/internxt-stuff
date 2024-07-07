# upload-internxt.py
A script to recursive upload to internxt

1. The script begins with a shebang (#\!/usr/bin/env python3) to indicate that it should be executed using Python 3.

2. Here are the instructions for the script:

Install the Internxt CLI globally using npm i -g @internxt/cli.
Log in using internxt login.
Select the remote directory to upload to using internxt list --id=\.\.\..
Finally, run the script: upload-internxt\.py <local-directory|local file> <remote directory id>.


3. The script defines several functions:

run_command(command): Executes a shell command and captures its output.
list_files_and_folders(folder_id): Lists files and folders in the specified Internxt folder.
upload_file(file_path, folder_id, existing_files): Uploads a local file to the specified folder if it doesn't already exist.
create_folder(folder_name, parent_id, existing_folders): Creates a new folder within the specified parent folder.
process_folder(folder_path, parent_id): Processes files within a local folder and uploads them to Internxt.


4. The script uses subprocess to interact with the Internxt CLI, creating pseudo-terminals and handling command execution.

5. It maintains a dictionary called failed_commands to track any unsuccessful commands.
