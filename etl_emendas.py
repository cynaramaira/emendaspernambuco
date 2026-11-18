import json
import os
from datetime import datetime, timezone, timedelta
import pandas as pd
import requests

TIMEZONE_PE = timezone(timedelta(hours=-3))
DATA_HOJE = datetime.now(TIMEZONE_PE).strftime("%Y-%m-%d")

DATASET_SLUG = "emendasparlamentaresestaduais"
CKAN_API_URL = f"https://dados.pe.gov.br/api/3/action/package_show?id={DATASET_SLUG}"

ARQUIVOS_CHAVE = {
    "tabela_completa_pix.csv": "PIX",
    "tabela_completa_direta.csv": "DIRETA",
    "tabela_completa_gtv.csv": "GTV",
}

def obter_urls_recursos():
    resposta = requests.get(CKAN_API_URL, timeout=30)
    resposta.raise_for_status()
    pacote = resposta.json().get("result", {})
    mapa_urls = {}
    for recurso in pacote.get("resources", []):
        url = recurso.get("url", "")
        nome_arquivo = url.split("/")[-1].lower()
        for chave in ARQUIVOS_CHAVE:
            if chave in nome_arquivo:
                mapa_urls[ARQUIVOS_CHAVE[chave]] = url
    return mapa_urls

def ler_csv_seguro(url):
    if not url:
        return pd.DataFrame()
    try:
        r = requests.get(url, timeout=30)
        # Decodifica como UTF-8 com fallback
        conteudo = r.content.decode("utf-8", errors="replace")
        return pd.read_csv(pd.io.common.StringIO(conteudo), sep=";")
    except Exception:
        try:
            return pd.read_csv(url, sep=";", encoding="latin1")
        except Exception:
            return pd.DataFrame()

# FUNÇÃO BLINDADA DE CONVERSÃO MONETÁRIA (NUNCA MAIS COME DECIMAIS)
def converter_moeda_segura(val):
    if val is None or pd.isna(val) or str(val).strip() == "" or str(val).lower() == "nan":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace("R$", "").replace("\xa0", "").replace(" ", "").strip()
    if not s:
        return 0.0
    # Se contém ponto de milhar e vírgula de centavos: ex: 1.234.567,89
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    # Se contém apenas vírgula decimal: ex: 1234,56
    elif "," in s:
        s = s.replace(",", ".")
    # Se contém apenas ponto decimal (ex: 1697267.81 ou 1000000.0), MANTÉM O PONTO!
    try:
        return float(s)
    except Exception:
        return 0.0

def limpar_mojibake(s):
    if not isinstance(s, str):
        return ""
    try:
        return s.encode("latin1").decode("utf-8")
    except Exception:
        return s

def processar_dados():
    urls = obter_urls_recursos()

    df_pix = ler_csv_seguro(urls.get("PIX"))
    df_direta = ler_csv_seguro(urls.get("DIRETA"))
    df_gtv = ler_csv_seguro(urls.get("GTV"))

    if not df_pix.empty:
        df_pix["modalidade"] = "Transferência Especial (PIX)"
    if not df_direta.empty:
        df_direta["modalidade"] = "Execução Direta"
    if not df_gtv.empty:
        df_gtv["modalidade"] = "Convênio/Parceria (GTV)"

    colunas_comuns = [
        "codigo_alepe", "autor", "municipio", "beneficiario", "tematica", 
        "objeto", "modalidade", "status_resolucao_tce", 
        "condicao_saneamento_impedimento_tecnico", "numero_empenho",
        "valor_empenhado", "valor_liquidado", "valor_pago"
    ]

    dfs_consolidados = []
    for base in [df_pix, df_direta, df_gtv]:
        if not base.empty:
            for col in colunas_comuns:
                if col not in base.columns:
                    base[col] = ""
            dfs_consolidados.append(base[colunas_comuns])

    if not dfs_consolidados:
        print("Nenhum dado encontrado.")
        return

    df_final = pd.concat(dfs_consolidados, ignore_index=True)

    # 1. Conversão numérica BLINDADA antes de transformar em string
    for col in ["valor_empenhado", "valor_liquidado", "valor_pago"]:
        df_final[col] = df_final[col].apply(converter_moeda_segura)

    # 2. Limpeza textual
    df_final = df_final.fillna("")
    for col in ["codigo_alepe", "autor", "municipio", "beneficiario", "tematica", "objeto", "modalidade", "status_resolucao_tce", "condicao_saneamento_impedimento_tecnico", "numero_empenho"]:
        df_final[col] = df_final[col].astype(str).apply(limpar_mojibake).str.strip()

    # Filtra linhas quebradas de metadados
    df_final = df_final[~df_final["codigo_alepe"].str.startswith("EMPENHO", na=False)]

    df_final["codigo_alepe"] = df_final["codigo_alepe"].replace({"": "SEM_COD", "nan": "SEM_COD"})
    df_final["autor"] = df_final["autor"].replace({"": "NÃO INFORMADO", "nan": "NÃO INFORMADO"}).str.upper()
    df_final["municipio"] = df_final["municipio"].replace({"": "Estadual / Não Regionalizado", "nan": "Estadual / Não Regionalizado"}).str.title()
    df_final["beneficiario"] = df_final["beneficiario"].replace({"": "Não Informado", "nan": "Não Informado", "-": "Não Informado"})
    df_final["tematica"] = df_final["tematica"].replace({"": "Outros", "nan": "Outros"}).str.upper()
    df_final["condicao_saneamento_impedimento_tecnico"] = df_final["condicao_saneamento_impedimento_tecnico"].replace({"-": "", "nan": ""})
    df_final["numero_empenho"] = df_final["numero_empenho"].replace({"-": "", "nan": ""})

    # 3. Lógica de status
    def definir_status(linha):
        st_tce = linha["status_resolucao_tce"].upper()
        if "DESCONTINUADA" in st_tce:
            return "Descontinuada"
        if linha["valor_pago"] > 0:
            return "Paga"
        if linha["valor_liquidado"] > 0:
            return "Liquidada (Aguardando Pagamento)"
        if linha["valor_empenhado"] > 0:
            return "Empenhada"
        if "IMPEDIMENTO" in st_tce or linha["condicao_saneamento_impedimento_tecnico"] != "":
            return "Impedimento Técnico"
        return "Indicada (Sem Execução)"

    df_final["status_calculado"] = df_final.apply(definir_status, axis=1)

    df_final["id_registro"] = (
        df_final["codigo_alepe"] + "_" + 
        df_final["modalidade"] + "_" + 
        df_final["municipio"]
    )

    lista_emendas = df_final.to_dict(orient="records")

    arquivo_saida = {
        "ultima_execucao": DATA_HOJE,
        "total_emendas": len(lista_emendas),
        "total_atualizadas_hoje": len(lista_emendas),
        "emendas": lista_emendas
    }

    with open("dados_emendas.json", "w", encoding="utf-8") as f:
        json.dump(arquivo_saida, f, ensure_ascii=False, indent=2, allow_nan=False)

    print(f"Base corrigida com sucesso: {len(lista_emendas)} emendas processadas.")

if __name__ == "__main__":
    processar_dados()
