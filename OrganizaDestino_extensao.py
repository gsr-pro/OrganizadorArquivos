# OrganizaDestino_Turbo.py
# Versão TURBO: extração segura + organização por extensão (com sharding) otimizada
# - Uma única organização no final
# - move_fast (os.replace) + sharding por hash
# - os.scandir + pipeline produtor-consumidor
# - anti-zip-bomb
# - rar multi-volume (só 1º volume)
# - logs enxutos para long runs

import os
import shutil
import zipfile
import rarfile
import logging
import csv
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
from queue import Queue
import hashlib
import errno

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
setup_logging(debug=False)  # deixe False para reduzir overhead em 9M+ arquivos

# ========================
# Auto-tuning de threads
# ========================
CPU = os.cpu_count() or 4
MAX_WORKERS_EXTRACT = min(8, max(3, CPU))         # 3..8
# mover/organizar geralmente aguenta mais; SSD/NVMe: até 32 costuma bem
MAX_WORKERS_MOVE    = min(32, max(8, 2 * CPU))    # 8..32
HEARTBEAT_SECONDS   = 30                           # log de progresso a cada Xs na rodada

logger.info(f"🧠 Auto-tuning: CPU={CPU} | MAX_WORKERS_EXTRACT={MAX_WORKERS_EXTRACT} | MAX_WORKERS_MOVE={MAX_WORKERS_MOVE}")

# ========================
# Segurança anti-zip-bomb
# ========================
MAX_UNCOMPRESSED_BYTES = 50 * 1024**3  # 50 GB por arquivo compactado (ajuste se quiser)

# ========================
# Pastas a ignorar (evita loops) + helpers
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
    return (
        d in SKIP_DIRS
        or d.endswith("_invalidos")
        or d.endswith("_pendentes_de_exclusao")
    )

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
    """Define caminho do CSV de erros e garante a pasta logs/"""
    global ERROR_LOG_DIR, ERROR_LOG_PATH, ERROR_LOG
    ERROR_LOG_DIR = os.path.join(destino_base, "logs")
    os.makedirs(ERROR_LOG_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ERROR_LOG_PATH = os.path.join(ERROR_LOG_DIR, f"erros_{stamp}.csv")
    ERROR_LOG = []

def log_error(etapa: str, acao: str, arquivo: str, destino: str, extensao: str, mensagem: str):
    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "etapa": etapa,
        "acao": acao,
        "arquivo": arquivo or "",
        "destino": destino or "",
        "extensao": extensao or "",
        "erro": (mensagem or "").replace("\n", " ").strip(),
    }
    with ERROR_LOCK:
        ERROR_LOG.append(row)

def save_error_log():
    if not ERROR_LOG_PATH or not ERROR_LOG:
        return
    campos = ["timestamp", "etapa", "acao", "arquivo", "destino", "extensao", "erro"]
    try:
        with open(ERROR_LOG_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=campos, delimiter=";")
            w.writeheader()
            w.writerows(ERROR_LOG)
        logger.info(f"🧾 Log de erros salvo: {ERROR_LOG_PATH}")
    except Exception as e:
        logger.warning(f"❌ Falha ao salvar log de erros: {e}")

# ========================
# RAR setup (Windows/Linux/macOS)
# ========================
def configurar_unrar():
    # Tenta caminhos padrão do WinRAR (Windows)
    candidatos = [
        r"C:\Program Files\WinRAR\UnRAR.exe",
        r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
    ] if os.name == "nt" else []

    for p in candidatos:
        if os.path.exists(p):
            rarfile.UNRAR_TOOL = p
            logger.debug(f"✅ RAR configurado (UnRAR.exe): {p}")
            return

    # Tenta ferramentas no PATH: apenas unrar/unar (bsdtar removido)
    for tool in ("unrar", "unar"):
        path = shutil.which(tool)
        if path:
            if tool == "unar":
                rarfile.UNAR_TOOL = path
            else:
                rarfile.UNRAR_TOOL = path
            logger.debug(f"✅ RAR configurado via PATH: {path}")
            return

    logger.warning("⚠️ Nenhum utilitário RAR encontrado (unrar/unar). Extração de .rar pode falhar.")

# ========================
# Helpers de filesystem
# ========================
DIR_LOCKS = defaultdict(threading.Lock)

def ext_folder_name(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower().strip('.')
    return (ext if ext else "outros").upper()

def ensure_folder(path: str):
    os.makedirs(path, exist_ok=True)

def get_unique_path(dest_dir: str, filename: str) -> str:
    """Gera caminho único sem sobrescrever (com lock por pasta)."""
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
    """Gera diretório único para extrair (ex.: foo_EXTR, foo_EXTR_(2), ...)."""
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
        logger.warning(f"🚫 {tipo.upper()} inválido movido para: {destino_final}")
    except Exception as e:
        logger.error(f"❌ Falha ao mover arquivo inválido '{nome}': {e}")
        log_error("mover_invalido", "mover", caminho_arquivo, destino_final, f".{tipo}", str(e))

def mover_para_pasta_pendentes_exclusao(caminho_arquivo: str, destino_base: str, tipo: str):
    """
    Move compactados que foram extraídos com sucesso mas não puderam ser EXCLUÍDOS.
    Pastas: ZIP_PENDENTES_DE_EXCLUSAO / RAR_PENDENTES_DE_EXCLUSAO
    """
    subpasta = f"{tipo.upper()}_PENDENTES_DE_EXCLUSAO"
    destino_pasta = os.path.join(destino_base, subpasta)
    ensure_folder(destino_pasta)
    nome = os.path.basename(caminho_arquivo)
    destino_final = get_unique_path(destino_pasta, nome)
    try:
        shutil.move(caminho_arquivo, destino_final)
        logger.warning(f"🟨 {tipo.upper()} movido para pendentes de exclusao: {destino_final}")
    except Exception as e:
        logger.error(f"❌ Falha ao mover pendente '{nome}': {e}")
        log_error("mover_pendente", "mover", caminho_arquivo, destino_final, f".{tipo}", str(e))

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
        # rarfile usa m.isdir()
        if not m.isdir():
            total += (m.file_size or 0)
            if total > MAX_UNCOMPRESSED_BYTES:
                raise Exception(f"RAR muito grande: {total/1024**3:.1f} GB")
    rf.extractall(base)

# ========================
# Limpeza de pastas vazias (útil pós-organização)
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
        except Exception as e:
            logger.debug(f"⚠️ Não foi possível remover {root}: {e}")

# ========================
# RAR multi-volume: somente 1º volume
# ========================
def _eh_primeiro_volume_rar(nome: str) -> bool:
    n = (nome or "").lower()
    if n.endswith(".part1.rar"):
        return True
    if ".part" in n and n.endswith(".rar"):
        return False
    return n.endswith(".rar")  # sem part => considera 1º

# ========================
# Extração recursiva (DESTINO) + exclusão do compactado
# ========================
def extrair_recursivo_e_limpar(destino_base: str, max_workers: int = MAX_WORKERS_EXTRACT):
    configurar_unrar()
    rodada = 0
    while True:
        compactados = []
        for root, dirs, files in os.walk(destino_base):
            # não descer nas pastas especiais
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

            # LOG de início (com tamanho do arquivo)
            try:
                zip_size = os.path.getsize(caminho)
            except Exception:
                zip_size = 0
            size_mb = zip_size / (1024 * 1024) if zip_size else 0.0
            logger.info(f"▶️ Iniciando extração: {nome} ({size_mb:.1f} MB) → {os.path.basename(out_dir)}")

            t0 = time.perf_counter()
            try:
                if caminho.lower().endswith(".zip"):
                    with zipfile.ZipFile(caminho, "r") as zf:
                        safe_extract_zip(zf, out_dir)
                else:
                    with rarfile.RarFile(caminho) as rf:
                        safe_extract_rar(rf, out_dir)

                # conclusão + estatística
                dt = max(0.001, time.perf_counter() - t0)
                mbps = (size_mb / dt) if size_mb > 0 else 0.0
                vtxt = f"{mbps:.1f} MB/s" if mbps > 0 else "n/a"
                logger.info(f"✅ Extraído: {nome} em {dt:.1f}s (~{vtxt}) → {os.path.basename(out_dir)}")

                # Tenta excluir o compactado original
                try:
                    _force_delete(caminho)
                    logger.debug(f"🗑️ Excluído compactado: {nome}")
                except Exception as e_del:
                    logger.warning(f"⚠️ Falha ao excluir '{nome}': {e_del}")
                    log_error("extrair", "remove", caminho, pasta_arquivo,
                              os.path.splitext(caminho)[1].lower(), str(e_del))
                    # mover para pendentes de exclusão e não reprocessar
                    mover_para_pasta_pendentes_exclusao(caminho, destino_base,
                                                        "zip" if caminho.lower().endswith(".zip") else "rar")
                return True

            except zipfile.BadZipFile as e_bad:
                mover_para_pasta_invalido(caminho, destino_base, "zip")
                logger.warning(f"❌ ZIP corrompido '{nome}': {e_bad}")
                log_error("extrair", "extract", caminho, out_dir, ".zip", f"corrompido: {e_bad}")
                return False
            except zipfile.LargeZipFile as e_lz:
                mover_para_pasta_invalido(caminho, destino_base, "zip")
                logger.warning(f"❌ ZIP muito grande/sem ZIP64 '{nome}': {e_lz}")
                log_error("extrair", "extract", caminho, out_dir, ".zip", f"ZIP64: {e_lz}")
                return False
            except rarfile.PasswordRequired:
                mover_para_pasta_invalido(caminho, destino_base, "rar")
                logger.warning(f"🔒 RAR protegido por senha '{nome}'")
                log_error("extrair", "extract", caminho, out_dir, ".rar", "senha requerida")
                return False
            except rarfile.NeedFirstVolume as e_nf:
                mover_para_pasta_invalido(caminho, destino_base, "rar")
                logger.warning(f"❌ RAR multi-volume faltando 1º volume '{nome}': {e_nf}")
                log_error("extrair", "extract", caminho, out_dir, ".rar", f"first volume: {e_nf}")
                return False
            except rarfile.BadRarFile as e_bad:
                mover_para_pasta_invalido(caminho, destino_base, "rar")
                logger.warning(f"❌ RAR corrompido '{nome}': {e_bad}")
                log_error("extrair", "extract", caminho, out_dir, ".rar", f"corrompido: {e_bad}")
                return False
            except rarfile.Error as e_rar:
                mover_para_pasta_invalido(caminho, destino_base, "rar")
                logger.warning(f"❌ Erro RAR '{nome}': {e_rar}")
                log_error("extrair", "extract", caminho, out_dir, ".rar", str(e_rar))
                return False
            except Exception as e:
                mover_para_pasta_invalido(caminho, destino_base,
                                          "zip" if caminho.lower().endswith(".zip") else "rar")
                logger.warning(f"❌ Erro ao extrair '{nome}': {e}")
                log_error("extrair", "extract", caminho, out_dir,
                          os.path.splitext(caminho)[1].lower(), str(e))
                return False

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(extrair, c) for c in compactados]
            total = len(futures)
            concluidos = 0
            last_hb = time.monotonic()
            for _ in as_completed(futures):
                concluidos += 1
                now = time.monotonic()
                if now - last_hb >= HEARTBEAT_SECONDS:
                    logger.info(f"⏱️ Andamento: {concluidos}/{total} compactados concluídos na rodada {rodada}")
                    last_hb = now

        # 🔥 TURBO: NÃO organizar aqui. Deixe para 1x no final.

# ========================
# Mover rápido + sharding
# ========================
def move_fast(src, dst_dir):
    """
    Move usando rename atômico na mesma partição.
    Se conflito de nome, tenta uma vez com sufixo de timestamp; se ainda assim colidir, cai para get_unique_path.
    """
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
            # raríssimo: garante via função única (com lock)
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

# Sharding: distribui arquivos em subpastas por hash do nome
def shard_subdir(nome: str) -> str:
    # 2 bytes de hash => 4 hex chars => 256x256 = 65.536 pastas
    h = hashlib.blake2b(nome.encode("utf-8"), digest_size=2).hexdigest()
    return os.path.join(h[:2], h[2:4])

def pasta_por_ext(nome: str, destino_base: str) -> str:
    pasta_tipo = ext_folder_name(nome)
    sub = shard_subdir(nome)  # comente esta linha para desativar sharding
    d = os.path.join(destino_base, pasta_tipo, sub)
    ensure_folder(d)
    return d

# ========================
# Organização – pipeline streaming (produtor-consumidor)
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
                        # não empilhar arquivos dentro de pastas especiais
                        if should_skip_dir(os.path.basename(os.path.dirname(e.path))):
                            continue
                        q.put(e.path)
                        total_listados += 1
                        now = time.monotonic()
                        if now - last_hb >= HEARTBEAT_SECONDS:
                            logger.info(f"🔎 Listados: ~{total_listados:,} arquivos")
                            last_hb = now
        except PermissionError:
            continue
        except FileNotFoundError:
            continue
        except Exception as e:
            log_error("listar", "scan", d, d, "", str(e))
            continue
    # sinaliza fim (o chamador envia N sentinelas)
    return total_listados

def worker_mover(q: Queue, destino_base: str, stats: dict, lock: threading.Lock):
    while True:
        p = q.get()
        if p is None:
            q.task_done()
            break
        try:
            # destino baseado na extensão + sharding
            nome = os.path.basename(p)
            destino_pasta = pasta_por_ext(nome, destino_base)
            # se já está dentro da pasta destino exata, ignora
            cur_dir = os.path.dirname(p)
            if os.path.abspath(cur_dir) != os.path.abspath(destino_pasta):
                _retry_move(p, destino_pasta)
            with lock:
                stats["moved"] += 1
        except Exception as e:
            log_error("organizar", "move", p, destino_pasta if 'destino_pasta' in locals() else "", os.path.splitext(p)[1].lower(), str(e))
            with lock:
                stats["errors"] += 1
        finally:
            q.task_done()

def organizar_streaming(destino_base: str, max_workers: int = MAX_WORKERS_MOVE):
    logger.info("🚚 Iniciando organização por extensão (modo streaming + sharding)")
    q = Queue(maxsize=10000)
    stats = {"moved": 0, "errors": 0}
    lock = threading.Lock()

    # inicia workers
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for _ in range(max_workers):
            ex.submit(worker_mover, q, destino_base, stats, lock)

        # produtor lista e empilha
        total_listados = listar_arquivos(destino_base, q)

        # manda sentinelas
        for _ in range(max_workers):
            q.put(None)

        # espera esvaziar
        q.join()

    logger.info(f"🏁 Organização concluída: listados ~{total_listados:,}, movidos {stats['moved']:,}, erros {stats['errors']:,}")

# ========================
# Contagem final (opcional – pode ser caro em 9M+)
# ========================
def contar_por_subpasta(destino_base: str, limit_report=False):
    contagem = defaultdict(int)
    for root, dirs, files in os.walk(destino_base):
        dirs[:] = [d for d in dirs if not should_skip_dir(d)]
        for nome in files:
            pasta_tipo = ext_folder_name(nome)
            contagem[pasta_tipo] += 1
    if limit_report:
        # evita spam em ambientes gigantes: mostra só top 10
        items = sorted(contagem.items(), key=lambda kv: kv[1], reverse=True)[:10]
        return dict(items)
    return contagem

def log_quantidade(contagem: dict):
    logger.info("\n📊 Quantidade (amostra) por subpasta:")
    if not contagem:
        logger.info("  (vazio)")
        return
    for tipo in sorted(contagem.keys()):
        logger.info(f"  • {tipo:<8}: {contagem[tipo]:,} arquivo(s)")

# ========================
# Processo principal (somente DESTINO)
# ========================
def executar_somente_destino(destino: str):
    if not os.path.isdir(destino):
        raise ValueError("Caminho de DESTINO inválido.")

    init_error_log(destino)

    logger.info("🚀 Iniciando (sem etapa de cópia; trabalhando apenas no DESTINO)")

    # 1) Extrair .zip/.rar recursivamente no DESTINO e excluir/mover pendentes
    extrair_recursivo_e_limpar(destino, max_workers=MAX_WORKERS_EXTRACT)

    # 2) ORGANIZAR POR EXTENSÃO (apenas 1x, modo streaming)
    organizar_streaming(destino, max_workers=MAX_WORKERS_MOVE)

    # 3) Limpar diretórios vazios pós-organização
    limpar_pastas_vazias(destino)

    # 4) Log final (opcionalmente limitado)
    contagem = contar_por_subpasta(destino, limit_report=True)
    log_quantidade(contagem)

    # 5) Salvar CSV de erros (se houver)
    save_error_log()
    logger.info("\n✅ Finalizado.")

# ========================
# MAIN
# ========================
if __name__ == "__main__":
    try:
        destino = input("📂 Caminho do DESTINO: ").strip()
        executar_somente_destino(destino)
    except KeyboardInterrupt:
        logger.warning("🛑 Interrompido pelo usuário (CTRL+C). Salvando log de erros...")
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
