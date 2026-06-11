
# OrganizaDestino_por_tipo.py
# Versão: organização por tipo de arquivo (XML, SPED, etc.) com classificação inteligente
# - Extrai recursivamente .zip/.rar
# - Classifica arquivos por tipo (especialmente XMLs fiscais)
# - Organiza em pastas por tipo com sharding

import os
import shutil
import zipfile
import rarfile
import logging
import csv
import re
import time
import hashlib
import errno
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from queue import Queue
import threading
from pathlib import Path

# Import lxml se disponível para XML parsing
try:
    from lxml import etree
    HAS_LXML = True
except ImportError:
    HAS_LXML = False
    etree = None

# ========================
# LOG (console)
# ========================
def setup_logging(debug=False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

logger = logging.getLogger(__name__)
setup_logging(debug=False)

# ========================
# Auto-tuning de threads
# ========================
CPU = os.cpu_count() or 4
MAX_WORKERS_EXTRACT = min(8, max(3, CPU))
MAX_WORKERS_MOVE = min(32, max(8, 2 * CPU))
MAX_WORKERS_CLASSIFY = min(16, max(4, CPU))
HEARTBEAT_SECONDS = 30

logger.info(f"🧠 Auto-tuning: CPU={CPU} | EXTRACT={MAX_WORKERS_EXTRACT} | MOVE={MAX_WORKERS_MOVE} | CLASSIFY={MAX_WORKERS_CLASSIFY}")

# ========================
# Segurança anti-zip-bomb
# ========================
MAX_UNCOMPRESSED_BYTES = 50 * 1024**3

# ========================
# Pastas a ignorar
# ========================
SKIP_DIRS = {
    "zip_invalidos",
    "rar_invalidos",
    "zip_pendentes_de_exclusao",
    "rar_pendentes_de_exclusao",
    "logs",
    "__macosx",
}

def should_skip_dir(dir_name: str) -> bool:
    d = (dir_name or "").lower()
    return d in SKIP_DIRS or d.endswith("_invalidos") or d.endswith("_pendentes_de_exclusao")

def is_inside_skipped(path: str, base: str) -> bool:
    abs_base = os.path.abspath(base)
    abs_path = os.path.abspath(path)
    if not abs_path.startswith(abs_base):
        return False
    rel = os.path.relpath(abs_path, abs_base)
    parts = rel.split(os.sep)
    return any(should_skip_dir(p) for p in parts if p not in (".",))

# ========================
# Error log (CSV) - thread-safe
# ========================
ERROR_LOG = []
ERROR_LOCK = threading.Lock()
ERROR_LOG_DIR = None
ERROR_LOG_PATH = None

def init_error_log(destino_base: str):
    global ERROR_LOG_DIR, ERROR_LOG_PATH, ERROR_LOG
    ERROR_LOG_DIR = os.path.join(destino_base, "logs")
    os.makedirs(ERROR_LOG_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ERROR_LOG_PATH = os.path.join(ERROR_LOG_DIR, f"erros_{stamp}.csv")
    ERROR_LOG = []

def log_error(etapa: str, acao: str, arquivo: str, destino: str, tipo: str, mensagem: str):
    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "etapa": etapa,
        "acao": acao,
        "arquivo": arquivo or "",
        "destino": destino or "",
        "tipo": tipo or "",
        "erro": (mensagem or "").replace("\n", " ").strip(),
    }
    with ERROR_LOCK:
        ERROR_LOG.append(row)

def save_error_log():
    if not ERROR_LOG_PATH or not ERROR_LOG:
        return
    campos = ["timestamp", "etapa", "acao", "arquivo", "destino", "tipo", "erro"]
    try:
        with open(ERROR_LOG_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=campos, delimiter=";")
            w.writeheader()
            w.writerows(ERROR_LOG)
        logger.info(f"🧾 Log de erros salvo: {ERROR_LOG_PATH}")
    except Exception as e:
        logger.warning(f"❌ Falha ao salvar log de erros: {e}")

# ========================
# RAR setup
# ========================
def configurar_unrar():
    candidatos = [
        r"C:\Program Files\WinRAR\UnRAR.exe",
        r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
    ] if os.name == "nt" else []
    for p in candidatos:
        if os.path.exists(p):
            rarfile.UNRAR_TOOL = p
            logger.debug(f"✅ RAR configurado (UnRAR.exe): {p}")
            return
    for tool in ("unrar", "unar"):
        path = shutil.which(tool)
        if path:
            if tool == "unar":
                rarfile.UNAR_TOOL = path
            else:
                rarfile.UNRAR_TOOL = path
            logger.debug(f"✅ RAR configurado via PATH: {path}")
            return
    logger.warning("⚠️ Nenhum utilitário RAR encontrado.")

# ========================
# Helpers de filesystem
# ========================
DIR_LOCKS = defaultdict(threading.Lock)

def ensure_folder(path: str):
    os.makedirs(path, exist_ok=True)

def get_unique_path(dest_dir: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    dest_dir_abs = os.path.abspath(dest_dir)
    with DIR_LOCKS[dest_dir_abs]:
        candidate = os.path.join(dest_dir, filename)
        i = 2
        while os.path.exists(candidate):
            candidate = os.path.join(dest_dir, f"{base}_({i}){ext}")
            i += 1
        return candidate

def get_unique_dir(dest_dir: str, base_name: str) -> str:
    dest_dir_abs = os.path.abspath(dest_dir)
    with DIR_LOCKS[dest_dir_abs]:
        cand = os.path.join(dest_dir, base_name)
        i = 2
        while os.path.exists(cand):
            cand = os.path.join(dest_dir, f"{base_name}_({i})")
            i += 1
        return cand

# ========================
# Pastas de inválidos/pendentes
# ========================
def mover_para_pasta_invalido(caminho_arquivo: str, destino_base: str, tipo: str):
    subpasta = f"{tipo.upper()}_INVALIDOS"
    destino_pasta = os.path.join(destino_base, subpasta)
    ensure_folder(destino_pasta)
    nome = os.path.basename(caminho_arquivo)
    destino_final = get_unique_path(destino_pasta, nome)
    try:
        shutil.move(caminho_arquivo, destino_final)
        logger.warning(f"🚫 {tipo.upper()} inválido movido: {destino_final}")
    except Exception as e:
        logger.error(f"❌ Falha ao mover arquivo inválido '{nome}': {e}")
        log_error("mover_invalido", "move", caminho_arquivo, destino_final, tipo, str(e))

def mover_para_pasta_pendentes_exclusao(caminho_arquivo: str, destino_base: str, tipo: str):
    subpasta = f"{tipo.upper()}_PENDENTES_DE_EXCLUSAO"
    destino_pasta = os.path.join(destino_base, subpasta)
    ensure_folder(destino_pasta)
    nome = os.path.basename(caminho_arquivo)
    destino_final = get_unique_path(destino_pasta, nome)
    try:
        shutil.move(caminho_arquivo, destino_final)
        logger.warning(f"🟨 {tipo.upper()} pendente: {destino_final}")
    except Exception as e:
        logger.error(f"❌ Falha ao mover pendente '{nome}': {e}")
        log_error("mover_pendente", "move", caminho_arquivo, destino_final, tipo, str(e))

# ========================
# Anti zip-slip + anti zip-bomb
# ========================
def safe_extract_zip(zf: zipfile.ZipFile, target_dir: str):
    base = os.path.realpath(target_dir)
    total = 0
    for m in zf.infolist():
        dest = os.path.realpath(os.path.join(base, m.filename))
        if not (dest == base or dest.startswith(base + os.sep)):
            raise Exception("Path traversal detectado no ZIP")
        if not m.is_dir():
            total += (m.file_size or 0)
            if total > MAX_UNCOMPRESSED_BYTES:
                raise Exception(f"ZIP muito grande: {total/1024**3:.1f} GB")
    zf.extractall(base)

def safe_extract_rar(rf: rarfile.RarFile, target_dir: str):
    base = os.path.realpath(target_dir)
    total = 0
    for m in rf.infolist():
        dest = os.path.realpath(os.path.join(base, m.filename))
        if not (dest == base or dest.startswith(base + os.sep)):
            raise Exception("Path traversal detectado no RAR")
        if not m.isdir():
            total += (m.file_size or 0)
            if total > MAX_UNCOMPRESSED_BYTES:
                raise Exception(f"RAR muito grande: {total/1024**3:.1f} GB")
    rf.extractall(base)

# ========================
# Limpeza de pastas vazias
# ========================
def limpar_pastas_vazias(destino_base: str):
    for root, dirs, files in os.walk(destino_base, topdown=False):
        if should_skip_dir(os.path.basename(root)):
            continue
        if os.path.abspath(root) == os.path.abspath(destino_base):
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
                logger.debug(f"🧹 Pasta vazia removida: {root}")
        except Exception:
            pass

# ========================
# RAR multi-volume
# ========================
def _eh_primeiro_volume_rar(nome: str) -> bool:
    n = (nome or "").lower()
    if n.endswith(".part1.rar"):
        return True
    if ".part" in n and n.endswith(".rar"):
        return False
    return n.endswith(".rar")

# ========================
# Extração recursiva
# ========================
def extrair_recursivo_e_limpar(destino_base: str, max_workers: int = MAX_WORKERS_EXTRACT):
    configurar_unrar()
    rodada = 0
    while True:
        compactados = []
        for root, dirs, files in os.walk(destino_base):
            dirs[:] = [d for d in dirs if not should_skip_dir(d)]
            for nome in files:
                low = nome.lower()
                if low.endswith(".zip") or (low.endswith(".rar") and _eh_primeiro_volume_rar(low)):
                    caminho = os.path.join(root, nome)
                    if not is_inside_skipped(caminho, destino_base):
                        compactados.append(caminho)
        if not compactados:
            break
        rodada += 1
        logger.info(f"📦 Compactados para extrair (rodada {rodada}): {len(compactados)}")

        def extrair(caminho: str):
            nome = os.path.basename(caminho)
            pasta_arquivo = os.path.dirname(caminho)
            nome_base = os.path.splitext(nome)[0]
            out_dir = get_unique_dir(pasta_arquivo, f"{nome_base}_EXTR")
            ensure_folder(out_dir)
            t0 = time.perf_counter()
            try:
                if caminho.lower().endswith(".zip"):
                    with zipfile.ZipFile(caminho, "r") as zf:
                        safe_extract_zip(zf, out_dir)
                else:
                    with rarfile.RarFile(caminho) as rf:
                        safe_extract_rar(rf, out_dir)
                dt = max(0.001, time.perf_counter() - t0)
                logger.info(f"✅ Extraído: {nome} em {dt:.1f}s")
                try:
                    os.remove(caminho)
                except Exception as e_del:
                    logger.warning(f"⚠️ Falha ao excluir '{nome}': {e_del}")
                    log_error("extrair", "remove", caminho, pasta_arquivo, os.path.splitext(caminho)[1].lower(), str(e_del))
                    mover_para_pasta_pendentes_exclusao(caminho, destino_base, "zip" if caminho.lower().endswith(".zip") else "rar")
                return True
            except Exception as e:
                mover_para_pasta_invalido(caminho, destino_base, "zip" if caminho.lower().endswith(".zip") else "rar")
                logger.warning(f"❌ Erro ao extrair '{nome}': {e}")
                log_error("extrair", "extract", caminho, out_dir, os.path.splitext(caminho)[1].lower(), str(e))
                return False

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(extrair, c) for c in compactados]
            concluidos = 0
            last_hb = time.monotonic()
            for _ in as_completed(futures):
                concluidos += 1
                now = time.monotonic()
                if now - last_hb >= HEARTBEAT_SECONDS:
                    logger.info(f"⏱️ Andamento: {concluidos}/{len(futures)}")
                    last_hb = now

# ========================
# CLASSIFICAÇÃO DE ARQUIVOS (XML, SPED, etc.)
# ========================

_NFSE_TAGS = frozenset({
    "InfDeclaracaoPrestacaoServico",
    "CompNfse",
    "GerarNfseEnvio",
    "NFSe",
    "infNFSe",
    "notafiscal",
})

_EVENTO_MARKERS = ("EVENTO", "RETEVENTO", "PROCEVENTO")
_PERIOD_FROM_PATH = re.compile(r"[/\\](20\d{2})[/\\-]?(0[1-9]|1[0-2])[/\\]")

def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag

def inferir_tipo_sped_por_0000(path: str) -> str | None:
    try:
        with open(path, "r", encoding="latin-1") as f:
            line = f.readline().strip()
            if line.startswith("|0000|"):
                parts = line.split("|")
                if len(parts) >= 6:
                    reg = parts[2].strip()
                    if reg == "ECD":
                        return "SPED_ECD"
                    elif reg == "ECF":
                        return "SPED_ECF"
                    elif reg == "EFD":
                        return "SPED_EFD"
                    elif reg == "FISS":
                        return "SPED_FISS"
                    else:
                        return "SPED_OUTRO"
    except Exception:
        pass
    return None

def classify_file_fast(path: str) -> str:
    try:
        with open(path, "rb") as f:
            chunk = f.read(4096)
            chunk_upper = chunk.upper()
            
            # XML: NFe, CTe
            if b"INFNFE" in chunk_upper:
                return "NFE"
            if b"INFCTE" in chunk_upper:
                return "CTE"
            
            # SPED
            first_pipe = chunk.find(b"|")
            if first_pipe != -1 and chunk[first_pipe:first_pipe+6] == b"|0000|":
                inferred = inferir_tipo_sped_por_0000(path)
                if inferred:
                    return inferred
            
            # NFSe - verificar primeiro para evitar confusão com tags como cep_evento
            has_nfse_tag = any(tag.upper().encode() in chunk_upper for tag in _NFSE_TAGS)
            if has_nfse_tag:
                if b"DPS" in chunk_upper or b"IBSCBS" in chunk_upper or b"_CBSIBS" in chunk_upper:
                    return "NFSE_NACIONAL"
                return "NFSE_MUNICIPAL_LEGADO"
            
            # Eventos - verificar apenas se não for NFSe
            for marker in _EVENTO_MARKERS:
                marker_bytes = marker.encode()
                # Verificar que o marker não está dentro de outra tag (como <cep_evento>)
                # Procurar por <EVENTO, >EVENTO, EVENTO>, ou EVENTO com espaço em branco ao redor
                if (b"<" + marker_bytes in chunk_upper or 
                    b">" + marker_bytes in chunk_upper or 
                    marker_bytes + b">" in chunk_upper or
                    b" " + marker_bytes + b" " in chunk_upper):
                    return "EVENTO"
    except Exception:
        pass
    
    # Fallback para lxml se for XML
    if not path.lower().endswith(".xml") or not HAS_LXML:
        ext = os.path.splitext(path)[1].lower().strip(".")
        return ext.upper() if ext else "OUTROS"
    
    root_tag = None
    is_nfse = False
    is_nacional = False
    try:
        for event, elem in etree.iterparse(path, events=("start", "end"), recover=True, huge_tree=True):
            tag = _xml_local_name(elem.tag)
            if event == "start":
                if root_tag is None:
                    root_tag = tag
                    root_upper = root_tag.upper()
                    if any(root_upper.startswith(m) or m in root_upper for m in _EVENTO_MARKERS):
                        return "EVENTO"
                if tag == "infNFe":
                    return "NFE"
                if tag == "infCte":
                    return "CTE"
                if tag in _NFSE_TAGS:
                    is_nfse = True
                if tag in ("DPS", "IBSCBS"):
                    is_nacional = True
                    if is_nfse:
                        return "NFSE_NACIONAL"
            elif event == "end":
                if tag in _NFSE_TAGS or tag == root_tag:
                    if is_nfse or is_nacional:
                        return "NFSE_NACIONAL" if is_nacional else "NFSE_MUNICIPAL_LEGADO"
                elem.clear()
    except Exception:
        pass
    
    if is_nfse:
        return "NFSE_NACIONAL" if is_nacional else "NFSE_MUNICIPAL_LEGADO"
    root_upper = (root_tag or "").upper()
    if any(root_upper.startswith(m) or m in root_upper for m in _EVENTO_MARKERS):
        return "EVENTO"
    
    ext = os.path.splitext(path)[1].lower().strip(".")
    return ext.upper() if ext else "OUTROS"

# ========================
# Mover rápido + sharding
# ========================
def move_fast(src, dst_dir):
    ensure_folder(dst_dir)
    nome = os.path.basename(src)
    dst = os.path.join(dst_dir, nome)
    try:
        os.replace(src, dst)
        return dst
    except FileExistsError:
        base, ext = os.path.splitext(nome)
        alt = os.path.join(dst_dir, f"{base}_{int(time.time()*1000)%1_000_000}{ext}")
        try:
            os.replace(src, alt)
            return alt
        except FileExistsError:
            final = get_unique_path(dst_dir, nome)
            os.replace(src, final)
            return final

def _force_delete(path):
    try:
        os.remove(path)
    except PermissionError:
        try:
            os.chmod(path, 0o666)
            os.remove(path)
        except Exception:
            raise

def _retry_move(src, dst_dir, attempts=2, delay=0.15):
    for i in range(attempts):
        try:
            return move_fast(src, dst_dir)
        except OSError as e:
            if i == attempts - 1 or e.errno not in (errno.EBUSY, errno.EACCES):
                raise
            time.sleep(delay)

def shard_subdir(nome: str) -> str:
    h = hashlib.blake2b(nome.encode("utf-8"), digest_size=2).hexdigest()
    return os.path.join(h[:2], h[2:4])

def pasta_por_tipo(nome: str, tipo_classificado: str, destino_base: str) -> str:
    pasta_tipo = tipo_classificado.upper()
    sub = shard_subdir(nome)
    d = os.path.join(destino_base, pasta_tipo, sub)
    ensure_folder(d)
    return d

# ========================
# Organização por tipo com classificação
# ========================
IGNORES_FILENAME = {"thumbs.db", ".ds_store", "desktop.ini"}

def listar_arquivos(root: str, q: Queue):
    stack = [root]
    skip = {s.lower() for s in SKIP_DIRS}
    total_listados = 0
    last_hb = time.monotonic()
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    name = e.name
                    if e.is_dir(follow_symlinks=False):
                        if not should_skip_dir(name):
                            stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        low = name.lower()
                        if low in IGNORES_FILENAME:
                            continue
                        if should_skip_dir(os.path.basename(os.path.dirname(e.path))):
                            continue
                        q.put(e.path)
                        total_listados += 1
                        now = time.monotonic()
                        if now - last_hb >= HEARTBEAT_SECONDS:
                            logger.info(f"🔎 Listados: ~{total_listados:,} arquivos")
                            last_hb = now
        except Exception:
            continue
    return total_listados

def worker_classificar_e_mover(q: Queue, destino_base: str, stats: dict, lock: threading.Lock):
    while True:
        p = q.get()
        if p is None:
            q.task_done()
            break
        try:
            nome = os.path.basename(p)
            tipo = classify_file_fast(p)
            destino_pasta = pasta_por_tipo(nome, tipo, destino_base)
            cur_dir = os.path.dirname(p)
            if os.path.abspath(cur_dir) != os.path.abspath(destino_pasta):
                _retry_move(p, destino_pasta)
            with lock:
                stats[tipo] = stats.get(tipo, 0) + 1
                stats["total"] = stats.get("total", 0) + 1
        except Exception as e:
            log_error("organizar", "move", p, destino_pasta if 'destino_pasta' in locals() else "", tipo if 'tipo' in locals() else "", str(e))
            with lock:
                stats["erros"] = stats.get("erros", 0) + 1
        finally:
            q.task_done()

def organizar_por_tipo_streaming(destino_base: str, max_workers: int = MAX_WORKERS_MOVE):
    logger.info("🚚 Iniciando organização por TIPO (modo streaming + sharding)")
    q = Queue(maxsize=10000)
    stats = {"total": 0, "erros": 0}
    lock = threading.Lock()
    
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for _ in range(max_workers):
            ex.submit(worker_classificar_e_mover, q, destino_base, stats, lock)
        
        total_listados = listar_arquivos(destino_base, q)
        
        for _ in range(max_workers):
            q.put(None)
        
        q.join()
    
    logger.info(f"🏁 Organização concluída: total={stats.get('total',0)}, erros={stats.get('erros',0)}")
    for k, v in sorted(stats.items()):
        if k not in ("total", "erros"):
            logger.info(f"  • {k:<20}: {v:,}")

# ========================
# Processo principal
# ========================
def executar_organizar_por_tipo(destino: str):
    if not os.path.isdir(destino):
        raise ValueError("Caminho de DESTINO inválido.")
    
    init_error_log(destino)
    logger.info("🚀 Iniciando organização POR TIPO")
    
    extrair_recursivo_e_limpar(destino, max_workers=MAX_WORKERS_EXTRACT)
    organizar_por_tipo_streaming(destino, max_workers=MAX_WORKERS_MOVE)
    limpar_pastas_vazias(destino)
    save_error_log()
    logger.info("\n✅ Finalizado.")

if __name__ == "__main__":
    try:
        destino = input("📂 Caminho do DESTINO: ").strip()
        executar_organizar_por_tipo(destino)
    except KeyboardInterrupt:
        logger.warning("🛑 Interrompido pelo usuário. Salvando log de erros...")
        try:
            save_error_log()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"❌ Erro: {e}")
        try:
            save_error_log()
        except Exception:
            pass

