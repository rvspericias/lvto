#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Streamlit – Extrator de Contracheques
Operator AI (GPT‑4o) + pdfplumber
"""

import os, io, re, json, pdfplumber, pandas as pd, streamlit as st, openai
from PIL import Image
import pytesseract

# ------------- CONFIG OPENAI -----------------
openai.api_key = os.getenv("OPENAI_API_KEY")
MODEL = "gpt-4o-mini"   # ajuste conforme seu plano

# ------------- PROMPT ------------------------
SYSTEM_PROMPT = """
Você é um assistente que extrai dados de contracheques brasileiros.
Retorne um JSON com:
{
  "mes_ano": "Mai/2024",
  "proventos": {"SALARIO": 1234.56, "OUTRO": 0.0},
  "base_fgts": 1234.56
}
Se não encontrar algo, use null.
Valores numéricos devem ser float com ponto decimal.
"""

# ------------- Funções -----------------------
def chamar_gpt(texto):
    msg = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": texto[:12000]}  # corta se > 12k tokens
    ]
    resp = openai.chat.completions.create(model=MODEL, messages=msg)
    content = resp.choices[0].message.content
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None

def processar_pdf(file_bytes, pagina_ini, pagina_fim):
    registros, rubricas, avisos = [], set(), []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        total_pag = len(pdf.pages)
        pagina_ini = max(1, pagina_ini)
        pagina_fim = min(total_pag, pagina_fim)

        for idx in range(pagina_ini-1, pagina_fim):
            page = pdf.pages[idx]
            texto = page.extract_text() or pytesseract.image_to_string(
                Image.open(io.BytesIO(page.to_image(resolution=300).original))
            )
            dados = chamar_gpt(texto)
            if not dados:
                avisos.append(f"Página {idx+1}: GPT não retornou JSON válido.")
                continue

            mes_ano = dados.get("mes_ano")
            if not mes_ano or any(r["Mês/Ano"] == mes_ano for r in registros):
                continue

            provs = {k.upper(): float(v) for k, v in (dados.get("proventos") or {}).items()}
            rubricas.update(provs.keys())
            registros.append({
                "Mês/Ano": mes_ano,
                "Proventos": provs,
                "Base FGTS": float(dados.get("base_fgts") or 0.0)
            })

    if not registros:
        return pd.DataFrame(), avisos

    rubricas = sorted(rubricas)
    linhas = []
    for reg in registros:
        linha = {"Mês/Ano": reg["Mês/Ano"], "Base FGTS": reg["Base FGTS"]}
        for rub in rubricas:
            linha[rub] = reg["Proventos"].get(rub, 0.0)
        linhas.append(linha)

    df = pd.DataFrame(linhas)
    df["Data"] = pd.to_datetime(df["Mês/Ano"], format="%b/%Y")
    df = df.sort_values("Data").drop(columns="Data")
    df = df[["Mês/Ano"] + rubricas + ["Base FGTS"]]
    return df, avisos

# ------------- STREAMLIT UI ------------------
st.set_page_config(page_title="Leitor de Contracheques (Operator AI)", layout="wide")
st.title("📑 Extrator de Contracheques – GPT‑4o")

arquivo = st.file_uploader("Arraste e solte o PDF", type=["pdf"])
col1, col2 = st.columns(2)
pagina_ini = col1.number_input("Página inicial", min_value=1, value=1)
pagina_fim = col2.number_input("Página final",  min_value=1, value=1)

if arquivo and st.button("Processar"):
    if not openai.api_key:
        st.error("Defina a variável de ambiente OPENAI_API_KEY.")
    else:
        with st.spinner("Processando…"):
            df, avisos = processar_pdf(arquivo.read(), pagina_ini, pagina_fim)
            if df.empty:
                st.error("Nenhum contracheque encontrado ou GPT não retornou dados.")
            else:
                st.success("Concluído!")
                st.dataframe(df, use_container_width=True)
                if avisos:
                    st.warning("⚠️ Revisar:\n" + "\n".join(f"- {a}" for a in avisos))
                buf = io.BytesIO()
                df.to_excel(buf, index=False)
                st.download_button("⬇️ Baixar Excel",
                                   buf.getvalue(),
                                   "contracheques.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
