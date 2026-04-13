FileSieve-Turbo 🌪️
FileSieve-Turbo é um motor de organização de arquivos de alta escala. Ele foi desenvolvido para lidar com cenários extremos (milhares ou milhões de arquivos), realizando a extração recursiva de compactados e a categorização por extensão em uma única passada otimizada.

🚀 Diferenciais da Versão Turbo
Sharding de Diretórios: Para evitar lentidão no sistema de arquivos ao abrir pastas com milhares de arquivos, o script utiliza Sharding por Hash (Blake2b). Os arquivos são distribuídos em subpastas hexadecimais (ex: JPG/a1/f2/foto.jpg), garantindo performance de leitura/escrita constante.

Extração Segura (Anti-ZipBomb): Proteção integrada contra ataques de descompressão infinita, limitando o tamanho total descompactado por arquivo.

Pipeline de Mover Rápido: Utiliza os.replace para renomeações atômicas, minimizando o overhead de I/O em discos SSD/NVMe.

Recuperação de Falhas de Compactação:

Invalidos: Arquivos corrompidos são movidos para pastas específicas para inspeção manual.

Pendentes: Compactados que foram extraídos com sucesso, mas que o sistema não permitiu deletar (arquivo em uso), são isolados para não gerar duplicidade.

Suporte a RAR Multi-volume: Identifica e processa apenas o primeiro volume (.part1.rar), evitando erros de extração duplicada.

🛠️ Arquitetura Técnica
O script opera em um modelo de Pipeline Multithread:

Scanner (Produtor): Varre o disco usando os.scandir (mais rápido que os.listdir) e alimenta uma fila de alta prioridade.

Workers (Consumidores): Um pool de threads consome a fila e realiza a lógica de movimentação e criação de diretórios em paralelo.

Auto-tuning: O script identifica os núcleos lógicos da CPU e equilibra:

Extração: Menos threads (processo pesado de CPU/IO).

Organização: Mais threads (processo leve de metadados).

📋 Como Configurar
Pré-requisitos
Python 3.7+

UnRAR Tool: Para suporte a arquivos .rar, é necessário ter o UnRAR.exe (Windows - geralmente em C:\Program Files\WinRAR) ou unrar/unar (Linux/Mac) instalado e no PATH.

Configurações Rápidas
No cabeçalho do arquivo, você pode ajustar:

MAX_UNCOMPRESSED_BYTES: Limite de segurança para extração (padrão 50GB).

shard_subdir: Comente esta função se preferir que os arquivos fiquem todos na raiz da pasta da extensão (não recomendado para mais de 10k arquivos por tipo).

📖 Fluxo de Operação
Extração: O script busca todos os .zip e .rar e os extrai "no lugar".

Recursividade: Se dentro de um ZIP houver outro ZIP, ele será detectado e extraído na rodada seguinte.

Sieve (Peneira): Todos os arquivos (originais e extraídos) são movidos para EXTENSAO/HASH1/HASH2/ARQUIVO.

Cleanup: Pastas vazias são removidas e um log CSV detalhado é gerado em caso de permissões negadas ou arquivos corrompidos.
