import json
import os
from datetime import datetime, timezone, timedelta
from collections import defaultdict
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
    """Consulta dinamicamente a API CKAN para obter os links mais recentes dos 3 recursos de 2026."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    resposta = requests.get(CKAN_API_URL, headers=headers, timeout=30)
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
    """Lê CSV do CKAN testando utf-8-sig e latin1."""
    if not url:
        return pd.DataFrame()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        resp = requests.get(url, headers=headers, timeout=45)
        resp.raise_for_status()
        try:
            return pd.read_csv(pd.io.common.BytesIO(resp.content), sep=None, engine="python", encoding="utf-8-sig", dtype=str)
        except Exception:
            return pd.read_csv(pd.io.common.BytesIO(resp.content), sep=";", encoding="latin1", dtype=str)
    except Exception as e:
        print(f"Erro ao carregar CSV de {url}: {e}")
        return pd.DataFrame()

def converter_moeda_segura(val):
    """Converte valores monetários com segurança preservando decimais reais."""
    if pd.isna(val) or val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace("R$", "").strip()
    if not s or s == "-" or s.lower() == "nan":
        return 0.0
    if "," in s:
        # Padrão brasileiro: ponto é milhar, vírgula é decimal
        s = s.replace(".", "").replace(",", ".")
    else:
        # Se só tem ponto, verifica se há múltiplos (separador de milhar) ou um só (ponto decimal)
        if s.count(".") > 1:
            s = s.replace(".", "")
    try:
        return float(s)
    except Exception:
        return 0.0

def processar_dados():
    urls = obter_urls_recursos()
    print("URLs obtidas via CKAN API:", urls)

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
            base.columns = [c.strip().lower() for c in base.columns]
            for col in colunas_comuns:
                if col not in base.columns:
                    base[col] = ""
            dfs_consolidados.append(base[colunas_comuns])

    if not dfs_consolidados:
        print("Nenhum dado encontrado para processar.")
        return

    df_final = pd.concat(dfs_consolidados, ignore_index=True)

    # 1. Limpeza textual e remoção de valores espúrios
    df_final = df_final.fillna("")
    for col in df_final.columns:
        df_final[col] = df_final[col].astype(str).str.strip()

    # Filtra linhas de metadados indevidas
    df_final = df_final[~df_final["codigo_alepe"].str.startswith("EMPENHO", na=False)]

    df_final["codigo_alepe"] = df_final["codigo_alepe"].replace({"": "SEM_COD", "nan": "SEM_COD"})
    df_final["autor"] = df_final["autor"].replace({"": "NÃO INFORMADO", "nan": "NÃO INFORMADO"}).str.upper()
    df_final["municipio"] = df_final["municipio"].replace({"": "Estadual / Não Regionalizado", "nan": "Estadual / Não Regionalizado"}).str.title()
    df_final["beneficiario"] = df_final["beneficiario"].replace({"": "Não Informado", "nan": "Não Informado", "-": "Não Informado"})
    df_final["tematica"] = df_final["tematica"].replace({"": "OUTROS", "nan": "OUTROS"}).str.upper()
    df_final["condicao_saneamento_impedimento_tecnico"] = df_final["condicao_saneamento_impedimento_tecnico"].replace({"-": "", "nan": ""})
    df_final["numero_empenho"] = df_final["numero_empenho"].replace({"-": "", "nan": ""})

    # 2. Conversão numérica segura (sem inflar casas decimais)
    for col in ["valor_empenhado", "valor_liquidado", "valor_pago"]:
        df_final[col] = df_final[col].apply(converter_moeda_segura)

    # 3. Consolidação por Emenda Parlamentar (agrupamento de múltiplos empenhos da mesma emenda)
    print(f"Total de registros brutos de empenho antes do agrupamento: {len(df_final)}")
    grupos = defaultdict(list)
    for item in df_final.to_dict(orient="records"):
        cod = item["codigo_alepe"]
        if cod in ("SEM_COD", "", "nan"):
            chave = f"SEM_COD_{item['modalidade']}_{item['municipio']}_{item['autor']}"
        else:
            chave = cod
        grupos[chave].append(item)

    emendas_consolidadas = []
    for chave, rows in grupos.items():
        base = rows[0]

        tot_emp = sum(r["valor_empenhado"] for r in rows)
        tot_liq = sum(r["valor_liquidado"] for r in rows)
        tot_pago = sum(r["valor_pago"] for r in rows)

        # Junta notas de empenho únicas
        empenhos = [r["numero_empenho"] for r in rows if r["numero_empenho"]]
        empenhos_unicos = list(dict.fromkeys(empenhos))
        empenho_str = ", ".join(empenhos_unicos) if empenhos_unicos else ""

        # Junta eventuais descrições de impedimento
        impedimentos = [r["condicao_saneamento_impedimento_tecnico"] for r in rows if r["condicao_saneamento_impedimento_tecnico"]]
        imp_str = " | ".join(list(dict.fromkeys(impedimentos)))

        # Status calculado consolidado
        st_tce = base["status_resolucao_tce"].upper()
        if "DESCONTINUADA" in st_tce:
            status = "Descontinuada"
        elif tot_pago > 0:
            status = "Paga"
        elif tot_liq > 0:
            status = "Liquidada (Aguardando Pagamento)"
        elif tot_emp > 0:
            status = "Empenhada"
        elif "IMPEDIMENTO" in st_tce or imp_str != "":
            status = "Impedimento Técnico"
        else:
            status = "Indicada (Sem Execução)"

        emenda_final = {
            "codigo_alepe": base["codigo_alepe"],
            "autor": base["autor"],
            "municipio": base["municipio"],
            "beneficiario": base["beneficiario"],
            "tematica": base["tematica"],
            "objeto": base["objeto"],
            "modalidade": base["modalidade"],
            "status_resolucao_tce": base["status_resolucao_tce"],
            "condicao_saneamento_impedimento_tecnico": imp_str,
            "numero_empenho": empenho_str,
            "qtd_empenhos": len(empenhos_unicos),
            "valor_empenhado": round(tot_emp, 2),
            "valor_liquidado": round(tot_liq, 2),
            "valor_pago": round(tot_pago, 2),
            "status_calculado": status,
            "id_registro": f"{base['codigo_alepe']}_{base['modalidade']}_{base['municipio']}"
        }
        emendas_consolidadas.append(emenda_final)

    print(f"Total de emendas consolidadas: {len(emendas_consolidadas)}")

    # 4. Leitura do histórico existente para rastreamento de mudanças diárias
    historico_anterior = {}
    caminho_json = "dados_emendas.json"
    if os.path.exists(caminho_json):
        try:
            with open(caminho_json, "r", encoding="utf-8") as f:
                dados_passados = json.load(f)
                for item in dados_passados.get("emendas", []):
                    historico_anterior[item["id_registro"]] = item
        except Exception:
            historico_anterior = {}

    lista_emendas = []
    atualizadas_hoje = 0

    for item in emendas_consolidadas:
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
                abs(item["valor_empenhado"] - float(registro_antigo.get("valor_empenhado", 0.0))) > 0.01 or
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

    # Salva o arquivo principal
    with open("dados_emendas.json", "w", encoding="utf-8") as f:
        json.dump(arquivo_saida, f, ensure_ascii=False, indent=2, allow_nan=False)

    # Se existir a pasta arquivosbrutos, salva também lá
    if os.path.exists("arquivosbrutos"):
        with open("arquivosbrutos/dados_emendas.json", "w", encoding="utf-8") as f:
            json.dump(arquivo_saida, f, ensure_ascii=False, indent=2, allow_nan=False)

    print(f"Base gerada com sucesso: {len(lista_emendas)} emendas únicas processadas ({atualizadas_hoje} modificadas hoje).")

if __name__ == "__main__":
    processar_dados()
