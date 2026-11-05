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
    "anulacao_empenho_pix.csv": "ANUL_EMP_PIX",
    "anulacao_empenho_direta.csv": "ANUL_EMP_DIRETA",
    "anulacao_empenho_gtv.csv": "ANUL_EMP_GTV",
    "ordem_bancaria_pix.csv": "OB_PIX",
    "ordem_bancaria_direta.csv": "OB_DIRETA",
    "ordem_bancaria_gtv.csv": "OB_GTV",
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

    # Limpeza e padronização
    df_final["codigo_alepe"] = df_final["codigo_alepe"].fillna("SEM_COD").astype(str).str.strip()
    df_final["autor"] = df_final["autor"].fillna("Não informado").str.upper().str.strip()
    df_final["municipio"] = df_final["municipio"].fillna("Estadual / Não regionalizado").str.title().str.strip()
    df_final["beneficiario"] = df_final["beneficiario"].fillna("Prefeitura / Não informado").str.strip()
    df_final["tematica"] = df_final["tematica"].fillna("Outros").str.strip()

    for col in ["valor_empenhado", "valor_liquidado", "valor_pago"]:
        df_final[col] = (
            df_final[col].astype(str)
            .str.replace("R$", "", regex=False)
            .str.replace(".", "", regex=False)
            .str.replace(",", ".", regex=False)
            .str.strip()
        )
        df_final[col] = pd.to_numeric(df_final[col], errors="coerce").fillna(0.0)

    # Identificação de impedimentos e status de execução
    def definir_status(linha):
        if str(linha["condicao_saneamento_impedimento_tecnico"]).strip() not in ["", "nan", "None"]:
            return "Impedimento Técnico"
        if linha["valor_pago"] > 0:
            return "Paga"
        if linha["valor_liquidado"] > 0:
            return "Liquidada (Aguardando Pagamento)"
        if linha["valor_empenhado"] > 0:
            return "Empenhada"
        return "Indicada (Sem Execução)"

    df_final["status_calculado"] = df_final.apply(definir_status, axis=1)

    # Chave única para rastrear modificações diárias
    df_final["id_registro"] = (
        df_final["codigo_alepe"] + "_" + 
        df_final["modalidade"] + "_" + 
        df_final["municipio"]
    )

    # Leitura do histórico do dia anterior para detecção de diff
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
                abs(item["valor_pago"] - registro_antigo.get("valor_pago", 0.0)) > 0.01 or
                abs(item["valor_liquidado"] - registro_antigo.get("valor_liquidado", 0.0)) > 0.01 or
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

    with open("dados_emendas.json", "w", encoding="utf-8") as f:
        json.dump(arquivo_saida, f, ensure_ascii=False, indent=2)

    print(f"Base gerada com sucesso: {len(lista_emendas)} emendas processadas ({atualizadas_hoje} modificadas hoje).")

if __name__ == "__main__":
    processar_dados()
