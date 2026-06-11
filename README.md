# Organizador de Arquivos - Nível Ouro (Tax Transformation)

## 🎯 Objetivo
O **Organizador de Arquivos** é uma ferramenta de alta performance construída para a área Fiscal/Tributária (Tax Transformation). O principal objetivo da aplicação é gerenciar, extrair, classificar e buscar **volumes massivos de arquivos fiscais** (XMLs de NFe/CTe/NFSe, arquivos SPED, PDFs, etc.) de forma rápida, segura e automatizada, sem que haja travamentos ou limites de limite de arquivos em pastas do Windows.

## 🧩 Módulos do Sistema

O sistema segue princípios de *Separation of Concerns* (Separação de Responsabilidades), dividindo a interface visual do processamento bruto:

1. **`app.py` (Frontend / Interface):**
   - É o ponto de entrada da aplicação.
   - Interface Gráfica moderna construída em **PySide6**.
   - Gerencia a interação do usuário, visualização de logs, preenchimento de formulários e exibição da tabela de resultados.

2. **`workers.py` (Backend / Assincronismo):**
   - Camada responsável por isolar processos demorados em threads em segundo plano (`QThread`), garantindo que a UI (Interface) nunca congele.
   - Contém a lógica de busca de altíssima performance usando `os.scandir` em formato de Pilha (Stack).
   - Gerencia operações de *I/O Bound* pesadas, como cópia de arquivos em lote e extração rápida de texto sem carregar árvores de XML inteiras na memória.

3. **`OrganizaDestino_extensao.py` (Motor de Extensão):**
   - Responsável por separar arquivos unicamente por suas extensões físicas (`.pdf`, `.xml`, `.xlsx`, etc.).
   - Processa a descompactação automática e recursiva de arquivos `.zip` e `.rar`.
   - Inclui rotinas de exclusão de pastas vazias pós-processamento.

4. **`OrganizaDestino_por_tipo.py` (Motor de Conteúdo Fiscal):**
   - Não depende da extensão do arquivo, ele analisa o **conteúdo** para classificar de forma contábil/fiscal.
   - Categoriza em: *NFe, CTe, NFSe (Nacional e Municipal), Eventos, SPED (ECD, ECF, EFD, FISS)*.

## 🏗️ Arquitetura de Dados e Performance

A arquitetura do projeto foi desenhada para o "nível enterprise" de manipulação de dados em disco, possuindo as seguintes tratativas técnicas:

* **Sharding (Fragmentação Inteligente):**
  A funcionalidade de sharding previne que o limite do File System (NTFS) ou o Windows Explorer travem tentando renderizar milhares de arquivos em um só lugar. A arquitetura detecta o volume e "fatia" (shards) os diretórios de saída automaticamente quando atingem um determinado limite de quantidade ou peso.
  
* **Descompactação Recursiva e Deep Search:**
  Os módulos não apenas leem a superfície da pasta. Eles penetram subdiretórios e conseguem "explodir" arquivos compactados de dentro de outros arquivos compactados (recursão de extração).

* **Varredura em Tempo Real (Stack-based Scandir):**
  Em vez do clássico (e lento) `os.walk` que armazena a árvore inteira na memória RAM antes de processar, o sistema de busca foi refatorado para utilizar iteradores de disco (`os.scandir`) acoplados a uma estrutura de dados de Pilha. O arquivo é analisado e descartado instantaneamente, proporcionando varreduras de **milhões de arquivos em segundos**, com consumo irrisório de RAM.

* **Stream de Regex Otimizado para XML:**
  O buscador de CNPJ / Chave de Acesso extrai buffers de strings diretamente usando codificação seletiva (UTF-8 com fallback para Latin-1) aplicando Regex apenas nos blocos numéricos. Ele não faz o "parse" formal de tags do XML, que seria milhares de vezes mais lento.

## 🚀 Como Executar
Basta ter o ambiente Python configurado com o `PySide6` e executar o app central:
```bash
python app.py
```
