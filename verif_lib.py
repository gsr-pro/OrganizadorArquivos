import os
import re
import subprocess
import sys
import pkgutil
import importlib.util

# --- CONFIGURAÇÕES ---

# Mapeamento de imports para nomes de pacotes no PyPI (corrigindo nomes comuns)
IMPORT_TO_PYPI = {
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn",
    "dateutil": "python-dateutil",
    "fitz": "pymupdf", 
    "pdf2image": "pdf2image",
    "OpenSSL": "pyopenssl",
    "usb": "pyusb",
    "serial": "pyserial",
    "dotenv": "python-dotenv",
    "git": "GitPython",
    "xlsxwriter": "XlsxWriter"
}

# Pacotes CRÍTICOS que DEVEM estar instalados (lista expandida do antigo parquet_lib_exe.py)
CRITICAL_PACKAGES = {
    "pandas", "pyarrow", "openpyxl", "pyodbc", "fastparquet", "Pillow", "numpy"
}

# --- FUNÇÕES ---

def verificar_e_instalar_pip():
    """Verifica se o pip está instalado e tenta instalá-lo caso não esteja."""
    print("🔍 Verificando pip...")
    try:
        subprocess.run([sys.executable, '-m', 'pip', '--version'], check=True, capture_output=True, text=True)
        print("✅ pip já está instalado.")
    except subprocess.CalledProcessError:
        print("⚠️ pip não encontrado! Tentando instalar...")
        try:
            subprocess.run([sys.executable, '-m', 'ensurepip', '--default-pip'], check=True)
            subprocess.run([sys.executable, '-m', 'pip', 'install', '--upgrade', 'pip'], check=True)
            print("✅ pip instalado com sucesso!")
        except Exception as e:
            print(f"❌ Erro crítico ao instalar o pip: {e}")

def obter_bibliotecas_padrao():
    """Retorna um set com os nomes das bibliotecas padrão do Python."""
    if hasattr(sys, 'stdlib_module_names'):
        return set(sys.stdlib_module_names)
    else:
        return {module.name for module in pkgutil.iter_modules() if module.module_finder is None}

def resolver_nome_pypi(nome_import):
    """Retorna o nome correto do pacote no PyPI dado o nome do import."""
    return IMPORT_TO_PYPI.get(nome_import, nome_import)

def instalar_pacote(nome_pacote):
    """Tenta instalar um pacote via pip."""
    print(f"📦 Instalando/Atualizando: {nome_pacote}...", end="", flush=True)
    try:
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--upgrade', nome_pacote], 
                       check=True, capture_output=True, text=True)
        print(" ✅")
        return True
    except subprocess.CalledProcessError as e:
        print(f" ❌ Falha (Erro: {e.stderr.strip() if e.stderr else 'Desconhecido'})")
        return False

def varrer_imports(caminho_pasta):
    """Varre arquivos .py recursivamente buscando imports."""
    imports_encontrados = set()
    # Regex ajustada para capturar 'import x' e 'from x import y'
    regex_import = re.compile(r'^\s*(?:from|import)\s+(\w+)', re.MULTILINE)
    
    print(f"\n🔎 Varrendo arquivos Python em: {caminho_pasta}")
    
    for root, _, arquivos in os.walk(caminho_pasta):
        # Ignorar pastas de ambiente virtual e cache
        if any(ignore in root for ignore in ['venv', '.git', '__pycache__', 'env']):
            continue
            
        for arquivo in arquivos:
            if arquivo.endswith('.py'):
                caminho_arquivo = os.path.join(root, arquivo)
                try:
                    with open(caminho_arquivo, 'r', encoding='utf-8', errors='ignore') as f:
                        conteudo = f.read()
                        matches = regex_import.findall(conteudo)
                        for match in matches:
                            imports_encontrados.add(match)
                except Exception as e:
                    print(f"⚠️ Erro ao ler {arquivo}: {e}")
                    
    return imports_encontrados

def teste_parquet():
    """Realiza um teste prático de criação e leitura de arquivo Parquet."""
    print("\n🧪 --- TESTE DE SANIDADE: PARQUET ---")
    try:
        import pandas as pd
        
        arquivo_teste = "teste_verif_lib.parquet"
        
        # Criar DataFrame simples
        df = pd.DataFrame({"coluna_a": [1, 2, 3], "coluna_b": ["x", "y", "z"]})
        
        # Teste de Escrita
        print("   📝 Gravando parquet...", end="")
        try:
            df.to_parquet(arquivo_teste) # tenta engine padrão (auto)
            print(" ✅ OK")
        except Exception as e:
            print(f" ❌ Erro na escrita: {e}")
            return False
        
        # Teste de Leitura
        print("   📖 Lendo parquet...", end="")
        try:
            df_lido = pd.read_parquet(arquivo_teste)
            if df.equals(df_lido):
                print(" ✅ OK (Dados conferem)")
            else:
                print(" ⚠️ OK (Mas dados divergem)")
        except Exception as e:
            print(f" ❌ Erro na leitura: {e}")
            return False
            
        print("🎉 TESTE PARQUET: SUCESSO!")
        
        # Limpeza
        if os.path.exists(arquivo_teste):
            try: os.remove(arquivo_teste)
            except: pass
            
        return True
        
    except ImportError:
        print("❌ Biblioteca pandas não encontrada para o teste.")
        return False
    except Exception as e:
        print(f"❌ Erro genérico no teste Parquet: {e}")
        return False

# --- MAIN ---

def main():
    caminho_atual = os.getcwd()
    print("🚀 Iniciando Verificação Completa e Atualização de Bibliotecas\n")
    
    verificar_e_instalar_pip()
    
    # 1. Obter bibliotecas padrão (para ignorar)
    libs_padrao = obter_bibliotecas_padrao()
    
    # 2. Varrer imports do projeto
    imports_projeto = varrer_imports(caminho_atual)
    print(f"   -> Encontrados {len(imports_projeto)} imports únicos.")
    
    # 3. Consolidar lista de pacotes
    pacotes_para_verificar = set()
    
    # Adicionar imports convertidos
    for imp in imports_projeto:
        if imp in libs_padrao:
            continue
        # Ignorar módulos locais (se existir arquivo .py com mesmo nome na raiz, assumimos local)
        if os.path.exists(os.path.join(caminho_atual, imp + ".py")):
            continue
            
        nome_pypi = resolver_nome_pypi(imp)
        pacotes_para_verificar.add(nome_pypi)
        
    # Adicionar críticos explicitamente
    for crit in CRITICAL_PACKAGES:
        pacotes_para_verificar.add(crit)
        
    print(f"\n📋 Total de pacotes externos identificados: {len(pacotes_para_verificar)}")
    
    # 4. Instalar/Atualizar
    sucessos = 0
    falhas = 0
    
    for pacote in sorted(pacotes_para_verificar):
        # Filtros básicos de falsos positivos
        if pacote.startswith('_') or pacote in ['os', 'sys', 're', 'time', 'math', 'gc', 'io']: 
            continue
            
        if instalar_pacote(pacote):
            sucessos += 1
        else:
            falhas += 1
            
    print(f"\n✨ Processo de bibliotecas finalizado ({sucessos} sucessos, {falhas} falhas).")
        
    # 5. Executar teste funcional do Parquet
    teste_parquet()
    
    print("\n🏁 Concluído.")

if __name__ == "__main__":
    main()
