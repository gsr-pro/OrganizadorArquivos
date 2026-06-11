
import OrganizaDestino_por_tipo as tipo_module

caminho_arquivo = r"C:\Users\gabriel.rocha\Desktop\Projetos\0. Projeção RTC\5.Grupo SIM\XML - 1 trim 2026\0. Teste Por tipo\EVENTO\00\02\NFSe41446-NFR.xml"
tipo = tipo_module.classify_file_fast(caminho_arquivo)
print(f"Classificação do arquivo: {tipo}")

