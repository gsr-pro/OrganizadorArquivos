import os
import re
import shutil
import logging
from PySide6.QtCore import QThread, Signal, QObject

# Classe para capturar logs e enviar para a interface
class LogHandler(QObject, logging.Handler):
    log_signal = Signal(str)

    def __init__(self):
        super().__init__()
        self.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

    def emit(self, record):
        msg = self.format(record)
        self.log_signal.emit(msg)

# Classe para rodar o processo em thread separada
class WorkerThread(QThread):
    finished = Signal()
    error = Signal(str)

    def __init__(self, func, path):
        super().__init__()
        self.func = func
        self.path = path

    def run(self):
        try:
            self.func(self.path)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self.finished.emit()

# Classe para busca XML em thread separada
class SearchWorkerThread(QThread):
    found_file = Signal(str)  # Caminho do arquivo encontrado
    finished = Signal()
    error = Signal(str)
    progress = Signal(int, int, int)  # Arquivos processados, total, encontrados

    def __init__(self, folder_path, chave_acesso, numero_doc, cnpj):
        super().__init__()
        self.folder_path = folder_path
        self.chave_acesso = chave_acesso.strip()
        self.numero_doc = numero_doc.strip()
        self.cnpj = cnpj.strip()

        # Pré-calcular critérios de busca limpos de caracteres não numéricos
        self.chave_limpa = re.sub(r"[^0-9]", "", self.chave_acesso)
        self.numero_limpo = re.sub(r"[^0-9]", "", self.numero_doc)
        self.cnpj_limpa = re.sub(r"[^0-9]", "", self.cnpj)

        # Pré-calcular o CNPJ ou CPF formatado padrão para busca flexível (ex: NFS-e municipais)
        self.cnpj_formatada = ""
        if len(self.cnpj_limpa) == 14:
            self.cnpj_formatada = f"{self.cnpj_limpa[:2]}.{self.cnpj_limpa[2:5]}.{self.cnpj_limpa[5:8]}/{self.cnpj_limpa[8:12]}-{self.cnpj_limpa[12:]}"
        elif len(self.cnpj_limpa) == 11:
            self.cnpj_formatada = f"{self.cnpj_limpa[:3]}.{self.cnpj_limpa[3:6]}.{self.cnpj_limpa[6:9]}-{self.cnpj_limpa[9:]}"

    def extract_text_from_xml(self, file_path):
        """Extrai todo o texto do XML para busca rápida (sem parse completo)"""
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read().upper()
        except Exception:
            try:
                with open(file_path, "r", encoding="latin-1", errors="ignore") as f:
                    return f.read().upper()
            except Exception:
                return ""

    def matches_criteria(self, xml_text):
        """Verifica se o XML corresponde aos critérios de busca de forma otimizada"""
        # Utiliza busca direta por substring, evitando o overhead drástico do re.sub no XML inteiro.
        
        # Verificar Chave de Acesso (se informada)
        if self.chave_limpa and (self.chave_limpa not in xml_text):
            return False

        # Verificar Número do Documento (se informado)
        if self.numero_limpo and (self.numero_limpo not in xml_text):
            return False

        # Verificar CNPJ (se informado)
        if self.cnpj_limpa:
            # Verifica o CNPJ no formato limpo e no formato formatado padrão
            if (self.cnpj_limpa not in xml_text) and (not self.cnpj_formatada or self.cnpj_formatada not in xml_text):
                return False

        return True

    def run(self):
        try:
            print(f"[DEBUG] Iniciando busca em: {self.folder_path}")
            scanned = 0
            processed = 0
            found_count = 0

            # Emitir status inicial
            self.progress.emit(0, -1, 0)

            # Usar scandir com stack para ser extremamente rápido e não travar lendo a lista de arquivos
            folders_to_scan = [self.folder_path]
            
            while folders_to_scan:
                current_folder = folders_to_scan.pop()
                try:
                    for entry in os.scandir(current_folder):
                        if entry.is_dir(follow_symlinks=False):
                            folders_to_scan.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            scanned += 1
                            
                            if entry.name.lower().endswith(".xml"):
                                file_path = entry.path
                                processed += 1

                                xml_text = self.extract_text_from_xml(file_path)
                                if self.matches_criteria(xml_text):
                                    found_count += 1
                                    self.found_file.emit(file_path)

                            # Atualizar a interface baseado na varredura (evita congelamento)
                            if scanned % 50 == 0:
                                self.progress.emit(processed, scanned, found_count)
                except PermissionError:
                    continue # Ignora pastas onde o usuário não tem permissão
                except Exception as e:
                    print(f"[DEBUG] Erro ao escanear pasta {current_folder}: {e}")

            # Garantir que emite o progresso final
            self.progress.emit(processed, scanned, found_count)

        except Exception as e:
            print(f"[DEBUG] Erro: {e}")
            self.error.emit(str(e))
        finally:
            self.finished.emit()

# Classe para copiar arquivos em thread separada
class CopyWorkerThread(QThread):
    copy_progress = Signal(int, int)  # Arquivos copiados, total
    finished = Signal()
    error = Signal(str)

    def __init__(self, file_list, dest_folder):
        super().__init__()
        self.file_list = file_list
        self.dest_folder = dest_folder

    def get_unique_filename(self, folder, filename):
        """Gera um nome de arquivo único caso já exista"""
        base, ext = os.path.splitext(filename)
        counter = 1
        new_name = filename
        while os.path.exists(os.path.join(folder, new_name)):
            new_name = f"{base}_{counter}{ext}"
            counter += 1
        return new_name

    def run(self):
        try:
            os.makedirs(self.dest_folder, exist_ok=True)
            total = len(self.file_list)
            copied = 0

            for file_path in self.file_list:
                if os.path.exists(file_path):
                    filename = os.path.basename(file_path)
                    dest_filename = self.get_unique_filename(self.dest_folder, filename)
                    dest_path = os.path.join(self.dest_folder, dest_filename)
                    shutil.copy2(file_path, dest_path)
                    copied += 1
                    self.copy_progress.emit(copied, total)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self.finished.emit()
