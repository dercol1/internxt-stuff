#!/usr/bin/env python3
# by Diego Ercolani 2024
# A simple approach to upload recursive directories to internxt
# using internxt cli (https://github.com/internxt/cli)
# you have only to
# 0. install internxt cli (npm i -g @internxt/cli)
# 1. login: internxt login
# 2. select the remote dir to upload to: internxt list --id=.....
# 3. launch the script:
#    upload-internxt.py <local-directory|local file> <remote directory id>
#  wait.....


#!/usr/bin/env python3
import os
import pty
import select
import subprocess
import sys
import pprint

failed_commands = {}
filesdict = {}
foldersdict = {}

def run_command(command):
    print(f'Running: {command}')

    # Crea un pseudo-terminale
    master, slave = pty.openpty()

    # Avvia il processo
    process = subprocess.Popen(
        command,
        shell=True,
        stdout=slave,
        stderr=slave,
        close_fds=True
    )

    # Chiudi il lato slave del pty nel processo padre
    os.close(slave)

    # Leggi l'output
    while True:
        try:
            # Attendi che ci sia output da leggere
            rlist, _, _ = select.select([master], [], [])
            if rlist:
                output = os.read(master, 1024).decode()
                if output:
                    sys.stdout.write(output)
                    sys.stdout.flush()
                else:
                    break
        except OSError:
            # Il processo figlio è terminato
            break

    # Chiudi il lato master del pty
    os.close(master)

    # Attendi che il processo termini e restituisci il codice di uscita
    return process.wait()

def list_files_and_folders(folder_id):
    destdir = ''
    for key, value in foldersdict.items():
        if value==folder_id:
            destdir=key
            break

    command = f'internxt list --id={folder_id}'
    print(command)
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        print(f"Errore nel listare i file: {stderr}", file=sys.stderr)
        return False

    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            obj_type = parts[0]
            obj_id = parts[-1]
            obj_name = ' '.join(parts[1:-1])
            if obj_type == "file":
                filesdict[f'{destdir}/{obj_name}'] = obj_id  # Nome file -> ID file
            elif obj_type == "folder":
                foldersdict[f'{destdir}/{obj_name}'] = obj_id  # Nome cartella -> ID cartella
            #else:
            #    print(filesdict)
            #    print(foldersdict)
            #    return False
    print(filesdict)
    return True


def upload_file(file_path, folder_id):
    # verifico se il file di destinazione esiste già
    # file_path
    # <destdir>/path/file_path

    for key, value in foldersdict.items():
        print(f'key: {key} value: {value}')
        if value==folder_id:
            destdir=key
            break;

    file_name=os.path.basename(file_path)
    file_dir=os.path.dirname(file_path)
    destfile=destdir+'/'+file_name
    if "." not in file_name:
        destfile+="."

    if destfile in filesdict.keys():
        print(f"File {file_path} già presente. Skip upload.")
        return

    print(f"Copia oggetto {file_path} in {folder_id} ({destdir})")

    command = f'internxt upload --id={folder_id} --file="{file_path}"'
    return_code = run_command(command)
    if return_code == 0:
        print(f'File "{file_path}" caricato con successo.')
    else:
        print(f"Errore nel caricamento del file {file_path}.")
        failed_commands[command] = "da ritornare errore upload"

def create_folder(folder_name, parent_id):
    # verifica se la cartella esiste già
    for key, value in foldersdict.items():
        if parent_id==value:
            destdir=key
    print(f'creo {folder_name} in {parent_id} ({destdir})')
    if destdir+'/'+folder_name in foldersdict.keys():
        print(f'Cartella "{folder_name}" già presente. Skip creation.')
        return foldersdict[destdir+'/'+folder_name]

    command = f'internxt create-folder --id={parent_id} --name="{folder_name}"'
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = process.communicate()
    if process.returncode == 0 and "Folder" in stdout:
        new_folder_id = stdout.split("folder/")[1].strip()
        print(stdout.strip())
        foldersdict[destdir+'/'+folder_name]=new_folder_id
        return new_folder_id
    else:
        print(f'Errore nella creazione della cartella "{folder_name}": {stderr}', file=sys.stderr)
        failed_commands[command] = stderr
        return None

def process_folder(local_folder_path, dest_foolder_id):
    print(f">>>>>>>process_folder su {local_folder_path} {dest_foolder_id}")
    if not list_files_and_folders(dest_foolder_id):
        print(f"Errore nel listare i file o le cartelle nella cartella con ID {dest_foolder_id}.")
        return
    else:
        print(f'La kettura della cartella id {dest_foolder_id} non ha ritornato errori')

    for item in os.listdir(local_folder_path):
        item_path=os.path.join(local_folder_path, item)

        if os.path.isfile(item_path):
             #print(f'upload_file({item_path},{dest_foolder_id})')
             upload_file(item_path, dest_foolder_id)

        # Crea la sottocartella e processala ricorsivamente
        if os.path.isdir(item_path):
            new_folder_id = create_folder(item, dest_foolder_id)
            if new_folder_id:
                    print(f"process_folder: {local_folder_path}/{item}, {new_folder_id}")
                    process_folder(local_folder_path+'/'+item, new_folder_id)
        print(f"esco da process_folder {local_folder_path}<<<<<<<<<<")

def main():
    if len(sys.argv) != 3:
        print("Uso: python upload-internxt.py <file/cartella> <folder_id>")
        return

    path = sys.argv[1]
    folder_id = sys.argv[2]
    foldersdict['<destfolder>']=folder_id

    if not os.path.exists(path):
        print(f"Il percorso {path} non esiste.")
        return

    if os.path.isdir(path):
        process_folder(path, folder_id)
    else:
        print(f"{path} non è una cartella valida.")

    print('Elenco domandi falliti')
    for key in failed_commands:
        print(key)

if __name__ == "__main__":
    main()
#!/usr/bin/env python3
import os
import pty
import select
import subprocess
import sys
import pprint

failed_commands = {}
folder_names = {}

def run_command(command):
    print(f'Running: {command}')

    # Crea un pseudo-terminale
    master, slave = pty.openpty()

    # Avvia il processo
    process = subprocess.Popen(
        command,
        shell=True,
        stdout=slave,
        stderr=slave,
        close_fds=True
    )

    # Chiudi il lato slave del pty nel processo padre
    os.close(slave)

    # Leggi l'output
    while True:
        try:
            # Attendi che ci sia output da leggere
            rlist, _, _ = select.select([master], [], [])
            if rlist:
                output = os.read(master, 1024).decode()
                if output:
                    sys.stdout.write(output)
                    sys.stdout.flush()
                else:
                    break
        except OSError:
            # Il processo figlio è terminato
            break

    # Chiudi il lato master del pty
    os.close(master)

    # Attendi che il processo termini e restituisci il codice di uscita
    return process.wait()

def list_files_and_folders(folder_id):
    command = f'internxt list --id={folder_id}'
    print(command)
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        print(f"Errore nel listare i file: {stderr}", file=sys.stderr)
        return None

    files = {}
    folders = {}

    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            obj_type = parts[0]
            obj_id = parts[-1]
            obj_name = ' '.join(parts[1:-1])
            if obj_type == "file":
                files[obj_name] = obj_id  # Nome file -> ID file
            elif obj_type == "folder":
                folders[obj_name] = obj_id  # Nome cartella -> ID cartella
                print(f'folder_names[{obj_id}]={obj_name}')
                folder_names[obj_id] = obj_name  # ID cartella -> Nome cartella
    #pprint.pprint(folder_names)
    return files, folders, folder_names


def upload_file(file_path, folder_path, folder_id, existing_files, folder_names):
    file_name = os.path.basename(file_path)
    if file_name in existing_files:
        print(f"File {file_name} già presente. Skip upload.")
        return

    remote_folder_name = folder_names.get(folder_id, 'sconosciuta')
    print(f"Copia oggetto {file_path} in {folder_id} ({folder_path})")

    command = f'internxt upload --id={folder_id} --file="{file_path}"'
    return_code = run_command(command)
    if return_code == 0:
        print(f'File "{file_path}" caricato con successo.')
    else:
        print(f"Errore nel caricamento del file {file_path}.")
        failed_commands[command] = "da ritornare errore upload"

def create_folder(folder_name, parent_id, existing_folders):
    if folder_name in existing_folders:
        print(f'Cartella "{folder_name}" già presente. Skip creation.')
        return existing_folders[folder_name]

    command = f'internxt create-folder --id={parent_id} --name="{folder_name}"'
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = process.communicate()
    if process.returncode == 0 and "Folder" in stdout:
        new_folder_id = stdout.split("folder/")[1].strip()
        folder_names[new_folder_id] = folder_name  # Aggiorna qui il folder_names
        print(stdout.strip())
        return new_folder_id
    else:
        print(f'Errore nella creazione della cartella "{folder_name}": {stderr}', file=sys.stderr)
        failed_commands[command] = stderr
        return None

def process_folder(folder_path, parent_id):
    existing_files, existing_folders, folder_names = list_files_and_folders(parent_id)
    if existing_files is None or existing_folders is None:
        print(f"Errore nel listare i file o le cartelle nella cartella con ID {parent_id}.")
        return

    for root, dirs, files in os.walk(folder_path):
        # Carica tutti i file nella cartella corrente
        for file in files:
            file_path = os.path.join(root, file)
            upload_file(file_path, folder_path, parent_id, existing_files, folder_names)

        # Crea tutte le sottocartelle e processale ricorsivamente
        for dir in dirs:
            dir_path = os.path.join(root, dir)
            new_folder_id = create_folder(dir, parent_id, existing_folders)
            if new_folder_id:
                process_folder(dir_path, new_folder_id)

def main():
    if len(sys.argv) != 3:
        print("Uso: python upload-internxt.py <file/cartella> <folder_id>")
        return

    path = sys.argv[1]
    folder_id = sys.argv[2]

    if not os.path.exists(path):
        print(f"Il percorso {path} non esiste.")
        return

    if os.path.isdir(path):
        process_folder(path, folder_id)
    else:
        print(f"{path} non è una cartella valida.")

    print('Elenco domandi falliti')
    for key in failed_commands:
        print(key)

if __name__ == "__main__":
    main()
