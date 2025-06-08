#!/usr/bin/env python3
import os
import pty
import select
import subprocess
import sys
import json
import logging
import time
import shutil
import threading
import re
from pathlib import Path
from typing import Dict, Optional, Tuple, List
from datetime import datetime
from enum import Enum
from collections import deque

class FileStatus(Enum):
    PENDING = "da trasferire"
    UPLOADING = "in trasferimento"
    COMPLETED = "trasferito"
    ALREADY_EXISTS = "già presente"
    FAILED = "non trasferito"
    REMOTE_EXISTING = "presente remoto"

class FileInfo:
    def __init__(self, local_path: str, status: FileStatus = FileStatus.PENDING, error: str = "",
                 is_folder: bool = False, completion_time: str = "", is_remote: bool = False):
        self.local_path = local_path
        self.status = status
        self.error = error
        self.is_folder = is_folder
        self.completion_time = completion_time
        self.is_remote = is_remote

class FailedOperation:
    def __init__(self, operation_type: str, path: str, command: str, error: str, timestamp: datetime):
        self.operation_type = operation_type
        self.path = path
        self.command = command
        self.error = error
        self.timestamp = timestamp

class ErrorInterpreter:
    """Interpreta gli errori di internxt in messaggi user-friendly"""

    ERROR_MAPPINGS = {
        "file already exists": "già presente",
        "already exists": "già presente",
        "permission denied": "permesso negato",
        "network error": "errore di rete",
        "timeout": "timeout",
        "insufficient space": "spazio insufficiente",
        "invalid file": "file non valido",
        "folder not found": "cartella non trovata",
        "authentication failed": "autenticazione fallita"
    }

    @classmethod
    def interpret_error(cls, error_message: str) -> str:
        """Interpreta un messaggio di errore in italiano"""
        if not error_message:
            return ""

        error_lower = error_message.lower()
        for english_error, italian_error in cls.ERROR_MAPPINGS.items():
            if english_error in error_lower:
                return italian_error

        return error_message

class InternxtUploader:
    def __init__(self, log_level=logging.INFO):
        # Setup debug logging se richiesto
        self.debug_mode = os.getenv('DEBUG') == '1'

        # Setup logging per errori terminale
        self.setup_logging(log_level)

        # Cache migliorata: folder_id -> {files: {filename: file_id}, folders: {foldername: folder_id}}
        self.remote_cache: Dict[str, Dict] = {}
        self.failed_operations: List[FailedOperation] = []

        # Tracking progresso
        self.all_files: List[FileInfo] = []
        self.current_file_index = 0
        self.total_files = 0
        self.source_path = ""
        self.target_path = ""

        # Display
        self.display_offset = 0
        self.diagnostic_messages = deque(maxlen=100)
        self.terminal_errors = deque(maxlen=50)

        # Navigazione
        self.input_thread = None
        self.stop_input = False
        self.navigation_enabled = False
        self.manual_scroll = False

        # Configurazione
        self.max_retries = 3
        self.retry_delay = 2

        # Calcola dimensioni console
        self.update_terminal_dimensions()

    def setup_logging(self, log_level):
        """Setup logging con file separato per errori terminale"""
        handlers = []
        if self.debug_mode:
            debug_filename = datetime.now().strftime("%y%m%d-%H%M-debug.log")
            handlers.append(logging.FileHandler(debug_filename))
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setFormatter(logging.Formatter('DEBUG: %(message)s'))
            handlers.append(console_handler)

        logging.basicConfig(
            level=log_level if self.debug_mode else logging.WARNING,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=handlers
        )
        self.logger = logging.getLogger(__name__)

        # Logger separato per errori terminale (sempre attivo)
        self.terminal_logger = logging.getLogger('terminal_errors')
        terminal_error_file = datetime.now().strftime("%y%m%d-%H%M-terminal-errors.log")
        terminal_handler = logging.FileHandler(terminal_error_file)
        terminal_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
        self.terminal_logger.addHandler(terminal_handler)
        self.terminal_logger.setLevel(logging.INFO)

    def log_terminal_error(self, error_msg: str):
        """Log degli errori del terminale"""
        self.terminal_logger.info(error_msg)
        self.terminal_errors.append(f"[{datetime.now().strftime('%H:%M:%S')}] {error_msg}")

    def update_terminal_dimensions(self):
        """Aggiorna le dimensioni del terminale"""
        try:
            size = shutil.get_terminal_size()
            self.terminal_height = max(20, size.lines)
            self.terminal_width = max(80, size.columns)
        except Exception as e:
            self.log_terminal_error(f"Errore get terminal size: {e}")
            self.terminal_height = 24
            self.terminal_width = 80

        # Calcola altezze sezioni
        available_lines = max(10, self.terminal_height - 6)
        self.file_section_height = max(5, available_lines // 3)  # Più spazio per i file
        self.diagnostic_section_height = available_lines - self.file_section_height

    def add_diagnostic(self, message: str):
        """Aggiunge un messaggio di diagnostica"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.diagnostic_messages.append(f"[{timestamp}] {message}")
        if self.debug_mode:
            self.logger.debug(f"DIAGNOSTIC: {message}")

    def clear_screen(self):
        """Pulisce lo schermo in modo sicuro"""
        try:
            if os.name == 'nt':
                os.system('cls')
            else:
                os.system('clear')
        except Exception as e:
            self.log_terminal_error(f"Errore clear screen: {e}")

    def get_display_files(self) -> List[FileInfo]:
        """Ottiene i file da mostrare nella finestra corrente"""
        if not self.all_files:
            return []

        # Auto-centra sul file corrente solo se non in scroll manuale
        if not self.manual_scroll and self.current_file_index > 0:
            # Trova il file attualmente in elaborazione
            current_file_path = None
            file_count = 0
            for file_info in self.all_files:
                if not file_info.is_folder and not file_info.is_remote:
                    file_count += 1
                    if file_count == self.current_file_index:
                        current_file_path = file_info.local_path
                        break

            if current_file_path:
                # Trova l'indice di questo file nella lista completa
                for idx, file_info in enumerate(self.all_files):
                    if file_info.local_path == current_file_path:
                        center_pos = self.file_section_height // 2
                        self.display_offset = max(0, idx - center_pos)
                        max_offset = max(0, len(self.all_files) - self.file_section_height)
                        self.display_offset = min(self.display_offset, max_offset)
                        break

        start_idx = self.display_offset
        end_idx = min(start_idx + self.file_section_height, len(self.all_files))
        return self.all_files[start_idx:end_idx]

    def format_path(self, path: str, max_length: int) -> str:
        """Formatta un percorso per la visualizzazione"""
        if len(path) <= max_length:
            return path
        return "..." + path[-(max_length-3):]

    def format_file_status_line(self, file_info: FileInfo, current_marker: str, type_marker: str, available_space: int) -> str:
        """Formatta una riga del file status con indicatori visivi migliorati"""
        # Indicatori di stato visivi
        status_indicators = {
            FileStatus.PENDING: "⏳",
            FileStatus.UPLOADING: "🔄",
            FileStatus.COMPLETED: "✅",
            FileStatus.ALREADY_EXISTS: "📁",
            FileStatus.FAILED: "❌",
            FileStatus.REMOTE_EXISTING: "💾"
        }

        # Stato base
        status_text = file_info.status.value
        status_icon = status_indicators.get(file_info.status, "⚪")

        # Aggiungi timestamp per file completati
        if file_info.status == FileStatus.COMPLETED and file_info.completion_time:
            status_text += f" {file_info.completion_time}"
        elif file_info.status == FileStatus.ALREADY_EXISTS and file_info.completion_time:
            status_text += f" {file_info.completion_time}"

        # Aggiungi errore interpretato per file falliti
        if file_info.status == FileStatus.FAILED and file_info.error:
            interpreted_error = ErrorInterpreter.interpret_error(file_info.error)
            status_text += f" ({interpreted_error})"

        # Calcola spazio per il percorso
        prefix_len = len(current_marker) + len(type_marker)
        status_len = len(status_icon) + len(status_text) + 1  # +1 per lo spazio
        path_space = available_space - prefix_len - status_len - 2

        # Formatta percorso
        display_path = file_info.local_path
        if file_info.is_remote:
            # Per file remoti, mostra solo il nome con indicatore
            display_path = f"(remoto) {Path(file_info.local_path).name}"

        path = self.format_path(display_path, max(15, path_space))

        return f"{current_marker}{type_marker}{path} {status_icon} {status_text}"

    def render_display(self):
        """Renderizza l'intero display"""
        try:
            # Aggiorna dimensioni se necessario
            self.update_terminal_dimensions()

            # Pulisce schermo
            self.clear_screen()

            # Header
            header_line = f"copia di {self.source_path} in {self.target_path}"
            if len(header_line) > self.terminal_width - 5:
                available = self.terminal_width - 15
                source_len = min(len(self.source_path), available // 2)
                target_len = available - source_len
                source_short = self.source_path if len(self.source_path) <= source_len else "..." + self.source_path[-(source_len-3):]
                target_short = self.target_path if len(self.target_path) <= target_len else "..." + self.target_path[-(target_len-3):]
                header_line = f"copia di {source_short} in {target_short}"

            print(header_line)
            print(f"{self.current_file_index}/{self.total_files}")

            # File Status
            display_files = self.get_display_files()
            if len(self.all_files) > self.file_section_height:
                start_num = self.display_offset + 1
                end_num = min(self.display_offset + self.file_section_height, len(self.all_files))
                print(f"\n--- File Status ({start_num}-{end_num}/{len(self.all_files)}) ---")
            else:
                print("\n--- File Status ---")

            # Lista file con indicatori migliorati
            for i, file_info in enumerate(display_files):
                actual_idx = self.display_offset + i

                # Indicatore file corrente (solo per file, non cartelle)
                is_current = False
                if not file_info.is_folder and not file_info.is_remote:
                    # Conta quanti file sono stati processati fino a questo punto
                    files_before = 0
                    for j in range(actual_idx):
                        if j < len(self.all_files) and not self.all_files[j].is_folder and not self.all_files[j].is_remote:
                            files_before += 1
                    is_current = (files_before + 1) == self.current_file_index

                current_marker = "→ " if is_current else "  "

                # Tipo elemento
                type_marker = "[DIR] " if file_info.is_folder else ""

                # Calcola spazio disponibile
                available_space = self.terminal_width - 3

                # Formatta riga con indicatori visivi
                line = self.format_file_status_line(file_info, current_marker, type_marker, available_space)
                print(line)

            # Riempi spazio vuoto nella sezione file
            displayed_files = len(display_files)
            for _ in range(self.file_section_height - displayed_files):
                print("")

            # Istruzioni navigazione (se necessarie)
            if len(self.all_files) > self.file_section_height:
                nav_text = "Navigazione: j/k ↑↓ (scroll), g/G (inizio/fine), f (segui), q (esci)"
                print(f"\n{nav_text}")
                self.try_enable_navigation()
            else:
                print()

            # Diagnostics
            print("\n--- Diagnostics ---")

            # Mostra messaggi diagnostici
            recent_messages = list(self.diagnostic_messages)[-self.diagnostic_section_height:]
            for message in recent_messages:
                print(message)

            # Riempi spazio rimanente
            displayed_diagnostics = len(recent_messages)
            for _ in range(self.diagnostic_section_height - displayed_diagnostics):
                print("")

            sys.stdout.flush()

        except Exception as e:
            self.log_terminal_error(f"Errore render display: {e}")

    def try_enable_navigation(self):
        """Prova ad abilitare la navigazione solo se non è già attiva"""
        if not self.navigation_enabled and (not self.input_thread or not self.input_thread.is_alive()):
            self.start_input_thread()

    def start_input_thread(self):
        """Avvia il thread per gestire l'input da tastiera"""
        try:
            self.stop_input = False
            self.manual_scroll = False
            self.input_thread = threading.Thread(target=self._input_loop, daemon=True)
            self.input_thread.start()
            self.navigation_enabled = True
            self.add_diagnostic("Navigazione interattiva attivata")
        except Exception as e:
            self.log_terminal_error(f"Errore start input thread: {e}")

    def _input_loop(self):
        """Loop principale per gestire l'input"""
        try:
            if os.name == 'nt':
                self._input_loop_windows()
            else:
                self._input_loop_unix()
        except Exception as e:
            self.log_terminal_error(f"Errore input loop: {e}")
        finally:
            self.navigation_enabled = False

    def _input_loop_windows(self):
        """Gestione input per Windows"""
        try:
            import msvcrt
            while not self.stop_input:
                if msvcrt.kbhit():
                    key = msvcrt.getch()
                    if key == b'\xe0':  # Tasti speciali
                        key2 = msvcrt.getch()
                        if key2 == b'H':  # Freccia su
                            self._handle_key('UP')
                        elif key2 == b'P':  # Freccia giù
                            self._handle_key('DOWN')
                    else:
                        char = key.decode('utf-8', errors='ignore')
                        self._handle_key(char)
                time.sleep(0.05)
        except ImportError:
            self.log_terminal_error("msvcrt non disponibile")
        except Exception as e:
            self.log_terminal_error(f"Errore input Windows: {e}")

    def _input_loop_unix(self):
        """Gestione input per Unix/Linux con fallback per diversi sistemi"""
        try:
            import termios
            import tty

            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)

            try:
                while not self.stop_input:
                    try:
                        if hasattr(tty, 'setcbreak'):
                            tty.setcbreak(fd)
                        elif hasattr(tty, 'cbreak'):
                            tty.cbreak(fd)
                        else:
                            new_settings = termios.tcgetattr(fd)
                            new_settings[3] = new_settings[3] & ~(termios.ECHO | termios.ICANON)
                            termios.tcsetattr(fd, termios.TCSADRAIN, new_settings)
                    except AttributeError as e:
                        self.log_terminal_error(f"Metodo tty non disponibile: {e}")
                        break

                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        char = sys.stdin.read(1)
                        if char == '\x1b':  # ESC sequence
                            if select.select([sys.stdin], [], [], 0.1)[0]:
                                char += sys.stdin.read(1)
                                if char == '\x1b[' and select.select([sys.stdin], [], [], 0.1)[0]:
                                    char += sys.stdin.read(1)
                                    if char == '\x1b[A':
                                        self._handle_key('UP')
                                    elif char == '\x1b[B':
                                        self._handle_key('DOWN')
                                    else:
                                        self._handle_key('ESC')
                                else:
                                    self._handle_key('ESC')
                            else:
                                self._handle_key('ESC')
                        else:
                            self._handle_key(char)
            finally:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except:
                    pass

        except Exception as e:
            self.log_terminal_error(f"Errore input Unix: {e}")

    def _handle_key(self, key: str):
        """Gestisce i tasti premuti"""
        try:
            self.manual_scroll = True

            if key in ['k', 'UP']:
                self.display_offset = max(0, self.display_offset - 1)
                self.render_display()
            elif key in ['j', 'DOWN']:
                max_offset = max(0, len(self.all_files) - self.file_section_height)
                self.display_offset = min(max_offset, self.display_offset + 1)
                self.render_display()
            elif key == 'g':
                self.display_offset = 0
                self.render_display()
            elif key == 'G':
                self.display_offset = max(0, len(self.all_files) - self.file_section_height)
                self.render_display()
            elif key == 'f':
                self.manual_scroll = False
                self.render_display()
            elif key in ['q', 'ESC']:
                self.stop_input = True
                self.add_diagnostic("Navigazione disattivata")
        except Exception as e:
            self.log_terminal_error(f"Errore handle key: {e}")

    def stop_input_thread(self):
        """Ferma il thread di input"""
        try:
            self.stop_input = True
            self.navigation_enabled = False
            if self.input_thread and self.input_thread.is_alive():
                self.input_thread.join(timeout=1.0)
        except Exception as e:
            self.log_terminal_error(f"Errore stop input thread: {e}")

    def update_file_status(self, file_path: str, status: FileStatus, error: str = "", completion_time: str = ""):
        """Aggiorna lo stato di un file"""
        try:
            updated = False
            for file_info in self.all_files:
                if file_info.local_path == file_path and not file_info.is_remote:
                    file_info.status = status
                    file_info.error = error
                    if completion_time:
                        file_info.completion_time = completion_time
                    updated = True
                    if self.debug_mode:
                        self.logger.debug(f"Stato aggiornato: {file_path} -> {status.value}")
                    break

            if not updated and self.debug_mode:
                self.logger.warning(f"File non trovato per aggiornamento stato: {file_path}")

            # Aggiorna display
            self.render_display()
        except Exception as e:
            self.log_terminal_error(f"Errore update file status: {e}")

    def extract_file_path_from_command(self, command: str) -> str:
        """Estrae il percorso del file dal comando internxt in modo sicuro"""
        # Cerca il pattern -f "percorso" o -f percorso
        match = re.search(r'-f\s+"([^"]+)"', command)
        if match:
            return match.group(1)

        # Fallback: cerca -f seguito da uno spazio e prende tutto fino alla fine
        match = re.search(r'-f\s+(.+)$', command)
        if match:
            path = match.group(1).strip('"')
            return path

        return "unknown_file"

    def detect_file_already_exists_error(self, output: str) -> bool:
        """Rileva se l'output contiene errore 'File already exists'"""
        return "file already exists" in output.lower() or "already exists" in output.lower()

    def run_command_json(self, command: str, timeout: int = 60) -> Tuple[bool, Optional[Dict], str]:
        """Esegue comando con output JSON"""
        json_command = f"{command} --json --non-interactive"
        if self.debug_mode:
            self.logger.debug(f'Comando JSON: {json_command}')

        try:
            result = subprocess.run(
                json_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            if result.returncode == 0:
                try:
                    data = json.loads(result.stdout) if result.stdout.strip() else {}
                    if self.debug_mode:
                        self.logger.debug(f'JSON Response: {data}')
                    return True, data, ""
                except json.JSONDecodeError:
                    return True, None, result.stdout
            else:
                error_msg = result.stderr.strip()
                if self.debug_mode:
                    self.logger.debug(f'JSON Command failed: {error_msg}')
                return False, None, error_msg

        except subprocess.TimeoutExpired:
            if self.debug_mode:
                self.logger.debug(f'JSON Command timeout: {json_command}')
            return False, None, "Timeout"
        except Exception as e:
            if self.debug_mode:
                self.logger.debug(f'JSON Command exception: {e}')
            return False, None, str(e)

    def run_command_with_pty(self, command: str, timeout: int = 300) -> Tuple[int, bool]:
        """Esegue comando con pseudo-terminale e rileva errori 'File already exists'"""
        # Estrazione corretta del nome file
        file_path = self.extract_file_path_from_command(command)
        file_name = Path(file_path).name

        if self.debug_mode:
            self.logger.info(f'Comando PTY: {command}')
            self.logger.info(f'File estratto: {file_path} -> nome: {file_name}')

        self.add_diagnostic(f"Esecuzione: {file_name}")

        file_already_exists = False

        try:
            master, slave = pty.openpty()
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=slave,
                stderr=slave,
                close_fds=True
            )
            os.close(slave)

            start_time = time.time()
            last_update = 0
            output_buffer = ""

            while True:
                if time.time() - start_time > timeout:
                    process.kill()
                    self.add_diagnostic("TIMEOUT")
                    return -1, False

                try:
                    rlist, _, _ = select.select([master], [], [], 1)
                    if rlist:
                        output = os.read(master, 1024).decode()
                        if output:
                            output_buffer += output

                            # Verifica se contiene errore "File already exists"
                            if self.detect_file_already_exists_error(output):
                                file_already_exists = True

                            # Aggiorna diagnostici con progresso
                            for line in output.split('\n'):
                                line = line.strip()
                                if line and any(keyword in line.lower() for keyword in ['uploading', 'progress', 'error', 'completed']):
                                    self.add_diagnostic(f"internxt: {line}")

                                    # Aggiorna display ogni 2 secondi
                                    current_time = time.time()
                                    if current_time - last_update > 2.0:
                                        self.render_display()
                                        last_update = current_time
                        else:
                            break
                except OSError:
                    break

            os.close(master)
            return_code = process.wait()

            if return_code == 0:
                self.add_diagnostic("✅ Completato con successo")
            else:
                self.add_diagnostic(f"❌ Fallito (codice {return_code})")

            if self.debug_mode:
                self.logger.info(f'Comando completato con codice: {return_code}, file_already_exists: {file_already_exists}')

            return return_code, file_already_exists

        except Exception as e:
            self.add_diagnostic(f"ERRORE: {e}")
            if self.debug_mode:
                self.logger.error(f'Errore esecuzione comando: {e}')
            return -1, False

    def validate_cli_availability(self) -> bool:
        """Verifica disponibilità CLI internxt"""
        try:
            result = subprocess.run(['internxt', '--version'],
                                  capture_output=True, text=True, timeout=10)
            available = result.returncode == 0
            if self.debug_mode:
                self.logger.info(f'CLI internxt disponibile: {available}')
            return available
        except Exception as e:
            if self.debug_mode:
                self.logger.error(f'Errore verifica CLI: {e}')
            return False

    def id_to_path(self, folder_id: str) -> str:
        """Converte ID cartella in percorso"""
        # Implementazione semplificata per ora
        return f"/{folder_id[:8]}"

    def collect_all_files(self, local_path: str) -> List[str]:
        """Raccoglie tutti i file"""
        files = []
        local_path_obj = Path(local_path)

        if local_path_obj.is_file():
            files.append(str(local_path_obj))
        elif local_path_obj.is_dir():
            for item in local_path_obj.rglob('*'):
                if item.is_file():
                    files.append(str(item))

        if self.debug_mode:
            self.logger.debug(f'Raccolti {len(files)} file da {local_path}')

        return files

    def collect_all_items(self, local_path: str) -> List[str]:
        """Raccoglie tutti gli elementi (file + cartelle) in ordine logico"""
        items = []
        local_path_obj = Path(local_path)

        if local_path_obj.is_file():
            items.append(str(local_path_obj))
        elif local_path_obj.is_dir():
            # Prima raccoglie tutte le cartelle, poi tutti i file
            all_paths = list(local_path_obj.rglob('*'))
            # Ordina per depth (cartelle più profonde prima per la creazione)
            all_paths.sort(key=lambda p: (len(p.parts), str(p)))

            # Aggiungi cartelle prima
            for item in all_paths:
                if item.is_dir():
                    items.append(str(item))

            # Poi aggiungi file
            for item in all_paths:
                if item.is_file():
                    items.append(str(item))

        if self.debug_mode:
            self.logger.debug(f'Raccolti {len(items)} elementi (file+cartelle) da {local_path}')

        return items

    def load_folder_contents_to_cache(self, folder_id: str) -> bool:
        """Carica il contenuto di una cartella nella cache migliorata"""
        if self.debug_mode:
            self.logger.debug(f'Caricamento cache per cartella: {folder_id}')

        success, data, error = self.run_command_json(f"internxt list -i {folder_id}")
        if not success:
            if self.debug_mode:
                self.logger.error(f'Errore lista cartella {folder_id}: {error}')
            return False

        # Inizializza cache per questa cartella
        self.remote_cache[folder_id] = {"files": {}, "folders": {}}

        if data and isinstance(data, dict):
            list_data = data.get('list', {})

            # Cache files
            files = list_data.get('files', [])
            for file_info in files:
                file_name = file_info.get('plainName', file_info.get('name', ''))
                file_id = file_info.get('uuid', file_info.get('id', ''))
                if file_name and file_id:
                    self.remote_cache[folder_id]["files"][file_name] = str(file_id)
                    if self.debug_mode:
                        self.logger.debug(f'Cached file: {folder_id}::{file_name} -> {file_id}')

            # Cache folders
            folders = list_data.get('folders', [])
            for folder_info in folders:
                folder_name = folder_info.get('plainName', folder_info.get('name', ''))
                sub_folder_id = folder_info.get('uuid', folder_info.get('id', ''))
                if folder_name and sub_folder_id:
                    self.remote_cache[folder_id]["folders"][folder_name] = str(sub_folder_id)
                    if self.debug_mode:
                        self.logger.debug(f'Cached folder: {folder_id}::{folder_name} -> {sub_folder_id}')

        if self.debug_mode:
            files_count = len(self.remote_cache[folder_id]["files"])
            folders_count = len(self.remote_cache[folder_id]["folders"])
            self.logger.debug(f'Cache caricata per {folder_id}: {files_count} file, {folders_count} cartelle')

        return True

    def file_exists_in_folder(self, file_name: str, folder_id: str) -> bool:
        """Verifica se un file esiste già in una cartella specifica"""
        # Assicurati che la cache sia caricata
        if folder_id not in self.remote_cache:
            if not self.load_folder_contents_to_cache(folder_id):
                return False

        file_exists = file_name in self.remote_cache[folder_id]["files"]
        if self.debug_mode:
            self.logger.debug(f'File {file_name} in {folder_id}: {"esiste" if file_exists else "non esiste"}')

        return file_exists

    def folder_exists_in_parent(self, folder_name: str, parent_id: str) -> Optional[str]:
        """Verifica se una cartella esiste già in un parent, ritorna l'ID se esiste"""
        # Assicurati che la cache sia caricata
        if parent_id not in self.remote_cache:
            if not self.load_folder_contents_to_cache(parent_id):
                return None

        folder_id = self.remote_cache[parent_id]["folders"].get(folder_name)
        if self.debug_mode:
            self.logger.debug(f'Cartella {folder_name} in {parent_id}: {"esiste" if folder_id else "non esiste"}')

        return folder_id

    def add_remote_files_to_display(self, folder_id: str, local_folder_path: str = ""):
        """Aggiunge i file già presenti nel server al display"""
        if folder_id not in self.remote_cache:
            if not self.load_folder_contents_to_cache(folder_id):
                return

        # Aggiungi file remoti al display
        for file_name in self.remote_cache[folder_id]["files"]:
            if local_folder_path:
                remote_file_path = f"{local_folder_path}/{file_name}"
            else:
                remote_file_path = file_name

            # Verifica se questo file non è già nella lista locale
            is_local = any(Path(f.local_path).name == file_name and not f.is_remote
                          for f in self.all_files)

            if not is_local:
                remote_file = FileInfo(
                    local_path=remote_file_path,
                    status=FileStatus.REMOTE_EXISTING,
                    is_remote=True
                )
                self.all_files.append(remote_file)
                if self.debug_mode:
                    self.logger.debug(f'Aggiunto file remoto al display: {remote_file_path}')

    def upload_file_with_retry(self, file_path: str, folder_id: str) -> bool:
        """Carica file con controllo preventivo e retry intelligente"""
        file_path_obj = Path(file_path)

        if not file_path_obj.exists() or not file_path_obj.is_file():
            self.update_file_status(file_path, FileStatus.FAILED, "file non valido")
            return False

        self.current_file_index += 1
        self.update_file_status(file_path, FileStatus.UPLOADING)

        # CONTROLLO PREVENTIVO MIGLIORATO
        if self.file_exists_in_folder(file_path_obj.name, folder_id):
            completion_time = datetime.now().strftime("%H:%M:%S")
            self.update_file_status(file_path, FileStatus.ALREADY_EXISTS, completion_time=completion_time)
            self.add_diagnostic(f"📁 File già presente (preventivo): {file_path_obj.name}")
            return True

        # Tentativo upload con retry intelligente
        for attempt in range(self.max_retries):
            if attempt > 0:
                self.add_diagnostic(f"🔄 Retry {attempt + 1}/{self.max_retries}: {file_path_obj.name}")
                time.sleep(self.retry_delay * attempt)

            command = f'internxt upload-file -i {folder_id} -f "{file_path}"'
            return_code, file_already_exists = self.run_command_with_pty(command)

            if return_code == 0:
                completion_time = datetime.now().strftime("%H:%M:%S")
                self.update_file_status(file_path, FileStatus.COMPLETED, completion_time=completion_time)
                self.add_diagnostic(f"✅ Upload completato: {file_path_obj.name} alle {completion_time}")
                # Aggiorna cache
                if folder_id not in self.remote_cache:
                    self.remote_cache[folder_id] = {"files": {}, "folders": {}}
                self.remote_cache[folder_id]["files"][file_path_obj.name] = "uploaded"
                return True
            elif return_code == 2 or file_already_exists:
                # File già esistente - FERMA I RETRY
                completion_time = datetime.now().strftime("%H:%M:%S")
                self.update_file_status(file_path, FileStatus.ALREADY_EXISTS, completion_time=completion_time)
                self.add_diagnostic(f"📁 File già presente (rilevato): {file_path_obj.name}")
                # Aggiorna cache
                if folder_id not in self.remote_cache:
                    self.remote_cache[folder_id] = {"files": {}, "folders": {}}
                self.remote_cache[folder_id]["files"][file_path_obj.name] = "existing"
                return True

            # Per altri tipi di errore, continua con i retry
            if self.debug_mode:
                self.logger.warning(f"Tentativo {attempt + 1} fallito con codice {return_code} per: {file_path}")

        # Controllo finale dopo tutti i tentativi
        if self.file_exists_in_folder(file_path_obj.name, folder_id):
            completion_time = datetime.now().strftime("%H:%M:%S")
            self.update_file_status(file_path, FileStatus.ALREADY_EXISTS, completion_time=completion_time)
            return True

        # Fallimento definitivo
        error_msg = f"fallito dopo {self.max_retries} tentativi"
        interpreted_error = ErrorInterpreter.interpret_error(error_msg)
        self.update_file_status(file_path, FileStatus.FAILED, interpreted_error)

        # Aggiungi alle operazioni fallite
        failed_op = FailedOperation("UPLOAD_FILE", file_path, command, error_msg, datetime.now())
        self.failed_operations.append(failed_op)

        return False

    def create_folder_safe(self, folder_name: str, parent_id: str, local_path: str = None) -> Optional[str]:
        """Crea cartella in modo sicuro con controllo preventivo"""
        if local_path:
            self.update_file_status(local_path, FileStatus.UPLOADING)

        # Controllo preventivo
        existing_id = self.folder_exists_in_parent(folder_name, parent_id)
        if existing_id:
            if local_path:
                completion_time = datetime.now().strftime("%H:%M:%S")
                self.update_file_status(local_path, FileStatus.ALREADY_EXISTS, completion_time=completion_time)
            self.add_diagnostic(f"📁 Cartella già presente: {folder_name}")
            return existing_id

        self.add_diagnostic(f"📁 Creazione cartella: {folder_name}")

        command = f'internxt create-folder -i {parent_id} --name "{folder_name}"'
        success, data, error = self.run_command_json(command)

        if success and data:
            folder_data = data.get('folder', {})
            new_folder_id = folder_data.get('uuid') or folder_data.get('id')

            if new_folder_id:
                completion_time = datetime.now().strftime("%H:%M:%S")
                self.add_diagnostic(f"✅ Cartella creata: {folder_name} alle {completion_time}")
                if local_path:
                    self.update_file_status(local_path, FileStatus.COMPLETED, completion_time=completion_time)

                # Aggiorna cache del parent
                if parent_id not in self.remote_cache:
                    self.remote_cache[parent_id] = {"files": {}, "folders": {}}
                self.remote_cache[parent_id]["folders"][folder_name] = str(new_folder_id)

                return str(new_folder_id)

        # Controlla di nuovo se esiste (race condition)
        existing_id = self.folder_exists_in_parent(folder_name, parent_id)
        if existing_id:
            if local_path:
                completion_time = datetime.now().strftime("%H:%M:%S")
                self.update_file_status(local_path, FileStatus.ALREADY_EXISTS, completion_time=completion_time)
            return existing_id

        # Fallimento
        interpreted_error = ErrorInterpreter.interpret_error(error or "creazione fallita")
        if local_path:
            self.update_file_status(local_path, FileStatus.FAILED, interpreted_error)
        self.add_diagnostic(f"❌ Errore creazione cartella: {folder_name} ({interpreted_error})")

        # Aggiungi alle operazioni fallite
        failed_op = FailedOperation("CREATE_FOLDER", local_path or folder_name, command, error or "creazione fallita", datetime.now())
        self.failed_operations.append(failed_op)

        return None

    def process_directory_recursive(self, local_path: str, remote_folder_id: str) -> bool:
        """Processa directory ricorsivamente"""
        local_path_obj = Path(local_path)

        if not local_path_obj.exists() or not local_path_obj.is_dir():
            return False

        if self.debug_mode:
            self.logger.debug(f'Processamento directory ricorsivo: {local_path} -> {remote_folder_id}')

        # Carica cache per questa cartella
        self.load_folder_contents_to_cache(remote_folder_id)

        # Aggiungi file remoti esistenti al display per questa cartella
        self.add_remote_files_to_display(remote_folder_id, str(local_path_obj))

        success = True

        try:
            # Processa prima le cartelle, poi i file
            items = list(local_path_obj.iterdir())

            # Prima le cartelle
            for item in items:
                if item.is_dir():
                    new_folder_id = self.create_folder_safe(item.name, remote_folder_id, str(item))
                    if new_folder_id:
                        if not self.process_directory_recursive(str(item), new_folder_id):
                            success = False
                    else:
                        success = False

            # Poi i file
            for item in items:
                if item.is_file():
                    if not self.upload_file_with_retry(str(item), remote_folder_id):
                        success = False

        except Exception as e:
            self.add_diagnostic(f"❌ ERRORE: {e}")
            if self.debug_mode:
                self.logger.error(f'Errore processamento directory: {e}')
            success = False

        return success

    def write_error_report(self):
        """Scrive report errori"""
        if not self.failed_operations:
            return

        source_path_obj = Path(self.source_path)
        base_dir = source_path_obj.parent if source_path_obj.is_file() else source_path_obj

        timestamp = datetime.now().strftime("%y%m%d-%H%M")
        error_file = base_dir / f"{timestamp}-internxt-errors.txt"

        try:
            with open(error_file, 'w', encoding='utf-8') as f:
                f.write("=== REPORT ERRORI INTERNXT ===\n")
                f.write(f"Generato: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Cartella sorgente: {self.source_path}\n")
                f.write(f"Cartella destinazione: {self.target_path}\n")
                f.write(f"Totale errori: {len(self.failed_operations)}\n\n")

                for i, failed_op in enumerate(self.failed_operations, 1):
                    f.write(f"--- ERRORE {i} ---\n")
                    f.write(f"Timestamp: {failed_op.timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"Tipo operazione: {failed_op.operation_type}\n")
                    f.write(f"Percorso: {failed_op.path}\n")
                    f.write(f"Comando: {failed_op.command}\n")
                    f.write(f"Errore: {failed_op.error}\n\n")

            print(f"\n📄 File di report errori creato: {error_file}")
        except Exception as e:
            print(f"❌ Errore nella scrittura del file di report: {e}")

    def print_summary(self):
        """Stampa riassunto finale"""
        self.stop_input_thread()

        print("\n=== RIASSUNTO OPERAZIONI ===")

        file_completed = sum(1 for f in self.all_files if not f.is_folder and not f.is_remote and f.status == FileStatus.COMPLETED)
        file_already_exists = sum(1 for f in self.all_files if not f.is_folder and not f.is_remote and f.status == FileStatus.ALREADY_EXISTS)
        file_failed = sum(1 for f in self.all_files if not f.is_folder and not f.is_remote and f.status == FileStatus.FAILED)
        file_remote_existing = sum(1 for f in self.all_files if not f.is_folder and f.is_remote and f.status == FileStatus.REMOTE_EXISTING)

        folder_completed = sum(1 for f in self.all_files if f.is_folder and not f.is_remote and f.status == FileStatus.COMPLETED)
        folder_already_exists = sum(1 for f in self.all_files if f.is_folder and not f.is_remote and f.status == FileStatus.ALREADY_EXISTS)
        folder_failed = sum(1 for f in self.all_files if f.is_folder and not f.is_remote and f.status == FileStatus.FAILED)

        print(f"📊 File totali da trasferire: {self.total_files}")
        print(f"  ✅ File trasferiti: {file_completed}")
        print(f"  📁 File già presenti: {file_already_exists}")
        print(f"  ❌ File falliti: {file_failed}")
        print(f"  💾 File presenti remoti: {file_remote_existing}")
        print(f"📁 Cartelle processate: {folder_completed + folder_already_exists + folder_failed}")
        print(f"  ✅ Cartelle create: {folder_completed}")
        print(f"  📁 Cartelle già presenti: {folder_already_exists}")
        print(f"  ❌ Cartelle fallite: {folder_failed}")

        if self.failed_operations:
            print(f"\n❌ === OPERAZIONI FALLITE ({len(self.failed_operations)}) ===")
            for failed_op in self.failed_operations:
                interpreted_error = ErrorInterpreter.interpret_error(failed_op.error)
                print(f"  [{failed_op.timestamp.strftime('%H:%M:%S')}] {failed_op.operation_type}: {failed_op.path}")
                print(f"      Errore: {interpreted_error}")
            self.write_error_report()
        else:
            print("\n✅ Nessun errore riscontrato!")

        # Debug info sulla cache
        if self.debug_mode:
            total_cached_files = sum(len(cache["files"]) for cache in self.remote_cache.values())
            total_cached_folders = sum(len(cache["folders"]) for cache in self.remote_cache.values())
            print(f"\n🔍 Cache info: {len(self.remote_cache)} cartelle, {total_cached_files} file, {total_cached_folders} sottocartelle")

        # Mostra info sui file errori terminale se creati
        if self.terminal_errors:
            print(f"\n🔧 Errori terminale registrati: {len(self.terminal_errors)} (vedi file di log)")

        if self.debug_mode:
            self.logger.info("=== RIASSUNTO OPERAZIONI COMPLETATO ===")

    def run(self, local_path: str, remote_folder_id: str) -> bool:
        """Funzione principale"""
        if not self.validate_cli_availability():
            print("❌ ERRORE: CLI internxt non disponibile")
            return False

        local_path_obj = Path(local_path)
        if not local_path_obj.exists():
            print(f"❌ ERRORE: Percorso non esistente: {local_path}")
            return False

        self.source_path = local_path
        self.target_path = self.id_to_path(remote_folder_id)

        if self.debug_mode:
            self.logger.info(f'🚀 Avvio trasferimento: {local_path} -> {remote_folder_id}')

        if local_path_obj.is_file():
            # File singolo
            all_file_paths = self.collect_all_files(local_path)
            self.total_files = len(all_file_paths)
            self.all_files = [FileInfo(path) for path in all_file_paths]

            # Carica cache e file remoti
            self.load_folder_contents_to_cache(remote_folder_id)
            self.add_remote_files_to_display(remote_folder_id)

            self.render_display()
            success = self.upload_file_with_retry(local_path, remote_folder_id)

        elif local_path_obj.is_dir():
            # Directory
            create_source_folder = not local_path.endswith(('/', '\\'))

            if create_source_folder:
                self.target_path = f"{self.id_to_path(remote_folder_id)}/{local_path_obj.name}"
                target_folder_id = self.create_folder_safe(local_path_obj.name, remote_folder_id, local_path)
                if not target_folder_id:
                    print(f"❌ ERRORE: Impossibile creare cartella di destinazione: {local_path_obj.name}")
                    return False
                target_folder_for_content = target_folder_id
            else:
                target_folder_for_content = remote_folder_id

            # Raccolta elementi in ordine logico
            all_file_paths = self.collect_all_files(local_path)
            self.total_files = len(all_file_paths)

            all_items = self.collect_all_items(local_path)
            self.all_files = []
            for item_path in all_items:
                is_folder = Path(item_path).is_dir()
                self.all_files.append(FileInfo(item_path, is_folder=is_folder))

            self.render_display()
            success = self.process_directory_recursive(local_path, target_folder_for_content)
        else:
            print(f"❌ ERRORE: Tipo di percorso non supportato: {local_path}")
            return False

        self.print_summary()
        return success


def main():
    if len(sys.argv) != 3:
        print("Uso: python upload-internxt.py <file/cartella> <folder_id>")
        print("Esempi:")
        print("  python upload-internxt.py /path/to/folder abc123def456")
        print("  python upload-internxt.py /path/to/folder/ abc123def456")
        print("  python upload-internxt.py /path/to/file.txt abc123def456")
        print("")
        print("Navigazione interattiva:")
        print("  j/k o ↑↓  : scorri su/giù")
        print("  g/G       : vai all'inizio/fine")
        print("  f         : segui il file corrente")
        print("  q         : esci dalla navigazione")
        print("")
        print("Simboli stato file:")
        print("  ⏳ da trasferire   🔄 in trasferimento   ✅ trasferito")
        print("  📁 già presente   ❌ errore            💾 presente remoto")
        print("")
        print("Imposta DEBUG=1 per logging dettagliato (solo su file)")
        return 1

    local_path = sys.argv[1]
    folder_id = sys.argv[2]

    log_level = logging.DEBUG if os.getenv('DEBUG') else logging.INFO
    uploader = InternxtUploader(log_level=log_level)

    try:
        success = uploader.run(local_path, folder_id)
        return 0 if success else 1
    except KeyboardInterrupt:
        print("\n\n⚡ Interrotto dall'utente")
        uploader.stop_input_thread()
        return 130
    except Exception as e:
        print(f"\n❌ ERRORE IMPREVISTO: {e}")
        uploader.stop_input_thread()
        return 1


if __name__ == "__main__":
    sys.exit(main())
