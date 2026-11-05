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
        return pd.read_csv(url, sep=None, engine="python", encoding="utf-8-sig")
    except Exception:
        try:
            return pd.read_csv(url, sep=";", encoding="latin1")
        except Exception:
            return pd.DataFrame()

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

    # 1. Limpeza textual e remoção de valores espúrios
    df_final = df_final.fillna("")
    for col in df_final.columns:
        df_final[col] = df_final[col].astype(str).str.strip()

    # Filtra linhas quebradas de metadados ou cabeçalhos repetidos
    df_final = df_final[~df_final["codigo_alepe"].str.startswith("EMPENHO", na=False)]

    df_final["codigo_alepe"] = df_final["codigo_alepe"].replace({"": "SEM_COD", "nan": "SEM_COD"})
    df_final["autor"] = df_final["autor"].replace({"": "NÃO INFORMADO", "nan": "NÃO INFORMADO"}).str.upper()
    df_final["municipio"] = df_final["municipio"].replace({"": "Estadual / Não Regionalizado", "nan": "Estadual / Não Regionalizado"}).str.title()
    df_final["beneficiario"] = df_final["beneficiario"].replace({"": "Não Informado", "nan": "Não Informado", "-": "Não Informado"})
    df_final["tematica"] = df_final["tematica"].replace({"": "Outros", "nan": "Outros"}).str.upper()
    df_final["condicao_saneamento_impedimento_tecnico"] = df_final["condicao_saneamento_impedimento_tecnico"].replace({"-": "", "nan": ""})
    df_final["numero_empenho"] = df_final["numero_empenho"].replace({"-": "", "nan": ""})

    # 2. Conversão numérica segura de valores
    for col in ["valor_empenhado", "valor_liquidado", "valor_pago"]:
        df_final[col] = (
            df_final[col]
            .str.replace("R$", "", regex=False)
            .str.replace(".", "", regex=False)
            .str.replace(",", ".", regex=False)
            .str.strip()
        )
        df_final[col] = pd.to_numeric(df_final[col], errors="coerce").fillna(0.0)

    # 3. Lógica orçamentária apurada de status
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

    # Chave única de identificação
    df_final["id_registro"] = (
        df_final["codigo_alepe"] + "_" + 
        df_final["modalidade"] + "_" + 
        df_final["municipio"]
    )

    # Leitura do histórico existente
    historico_anterior = {}
    if os.path.exists("dados_emendas.json"):
        try:
            with open("dados_emendas.json", "r", encoding="utf-8") as f:
                dados_passados = json.load(f)
                for item in dados_passados.get("emendas", []):
                    historico_anterior[item["id_registro"]] = item
        except Exception:
            historico_anterior = {}

    lista_emendas = []
    atualizadas_hoje = 0

    for item in df_final.to_dict(orient="records"):
        id_reg = item["id_registro"]
        registro_antigo = historico_anterior.get(id_reg)

        if not registro_antigo:
            item["data_modificacao"] = DATA_HOJE
            item["teve_modificacao_hoje"] = True
            item["detalhe_mudanca"] = "Nova emenda registrada"
            atualizadas_hoje += 1
        else:
            mudou_estagio = (
                abs(item["valor_pago"] - float(registro_antigo.get("valor_pago", 0.0))) > 0.01 or
                abs(item["valor_liquidado"] - float(registro_antigo.get("valor_liquidado", 0.0))) > 0.01 or
                item["status_calculado"] != registro_antigo.get("status_calculado")
            )
            if mudou_estagio:
                item["data_modificacao"] = DATA_HOJE
                item["teve_modificacao_hoje"] = True
                item["detalhe_mudanca"] = f"Novo status: {item['status_calculado']}"
                atualizadas_hoje += 1
            else:
                item["data_modificacao"] = registro_antigo.get("data_modificacao", DATA_HOJE)
                item["teve_modificacao_hoje"] = False
                item["detalhe_mudanca"] = ""

        lista_emendas.append(item)

    arquivo_saida = {
        "ultima_execucao": DATA_HOJE,
        "total_emendas": len(lista_emendas),
        "total_atualizadas_hoje": atualizadas_hoje,
        "emendas": lista_emendas
    }

    # Garante que nenhum NaN seja escrito no JSON final
    with open("dados_emendas.json", "w", encoding="utf-8") as f:
        json.dump(arquivo_saida, f, ensure_ascii=False, indent=2, allow_nan=False)

    print(f"Base gerada com sucesso: {len(lista_emendas)} emendas processadas ({atualizadas_hoje} modificadas hoje).")

if __name__ == "__main__":
    processar_dados()
