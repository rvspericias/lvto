#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web‑app Streamlit – Extração dinâmica de contracheques (SANOFI) com OCR fallback
"""

import re, io, pdfplumber, pandas as pd, streamlit as st, pytesseract
from PIL import Image

# ---------- Configuração ----------
st.set_page_config(page_title="Leitor de Contracheques", layout="wide")

# ---------- Expressões regulares ----------
re_ref  = re.compile(r"Refer[eê]ncia[:\s]+([A-ZÇ]+)\/(\d{4})", re.I)
re_fgts = re.compile(r"BASE\s+CALC\.\s+FGTS\s+([\d\.,]+)", re.I)

# ---------- Funções utilitárias ----------
def normalizar_valor(txt):
    txt = txt.strip()
    if not txt or txt in {"-", "0,00"}:
        return 0.0, True
    try:
        return float(txt.replace(".", "").replace(",", ".")), True
    except ValueError:
        return 0.0, False

def texto_pagina(page):
    """Extrai texto; se falhar, usa OCR."""
    texto = page.extract_text() or ""
    if texto.strip():
        return texto
    img: Image.Image = page.to_image(resolution=300).original
    return pytesseract.image_to_string(img, lang="por")

def extrair_recibo(page):
    avisos, texto = [], texto_pagina(page)
    linhas = texto.splitlines()

    # Mês/Ano
    mes_ano = None
    for ln in linhas[:8]:
        if (m := re_ref.search(ln)):
            mes, ano = m.groups()
            mes_ano = f"{mes[:3].title()}/{ano}"
            break
    if not mes_ano:
        return None

    # Proventos
    proventos, lendo = {}, False
    for ln in linhas:
        if ln.strip().startswith("Descrição"):
            lendo = True
            continue
        if lendo:
            if ln.strip().startswith("TOTAL DE PROVENTOS"):
                break
            partes = re.split(r"\s{2,}", ln.strip())
            if len(partes) >= 2:
                desc, valor_txt = partes[0].upper(), partes[-1]
                valor, ok = normalizar_valor(valor_txt)
                proventos[desc] = valor
                if not ok:
                    avisos.append(f"{mes_ano}: '{desc}' – valor não lido ({valor_txt})")

    # FGTS
    fgts_base = 0.0
    for ln in reversed(linhas):
        if (m := re_fgts.search(ln)):
            fgts_base, ok = normalizar_valor(m.group(1))
            if not ok:
                avisos.append(f"{mes_ano}: Base FGTS não reconhecida ({m.group(1)})")
            break
    else:
        avisos.append(f"{mes_ano}: Base FGTS não encontrada")

    return mes_ano, proventos, fgts_base, avisos

def processar_pdf(file_bytes, pagina_ini, pagina_fim):
    registros, rubricas, avisos_totais = [], set(), []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        total_pag = len(pdf.pages)
        pagina_ini = max(1, pagina_ini)
        pagina_fim = min(total_pag, pagina_fim)
        for idx in range(pagina_ini-1, pagina_fim):
            resultado = extrair_recibo(pdf.pages[idx])
            if resultado:
                mes_ano, provs, fgts, avisos = resultado
                if any(r["Mês/Ano"] == mes_ano for r in registros):
                    continue
                rubricas.update(provs.keys())
                registros.append({"Mês/Ano": mes_ano,
                                  "Proventos": provs,
                                  "Base FGTS": fgts})
                avisos_totais.extend(avisos)

    if not registros:
        return pd.DataFrame(), avisos_totais

    # Monta DataFrame
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
    return df, avisos_totais

# ---------- Interface ----------
st.title("📑 Extrator de Contracheques (SANOFI) – OCR Ready")

arquivo = st.file_uploader("Arraste e solte o PDF", type=["pdf"])
col1, col2 = st.columns(2)
pagina_ini = col1.number_input("Página inicial", min_value=1, value=1)
pagina_fim = col2.number_input("Página final",  min_value=1, value=1)

if arquivo and st.button("Processar"):
    with st.spinner("Processando…"):
        df, avisos = processar_pdf(arquivo.read(), pagina_ini, pagina_fim)
        if df.empty:
            st.error("Nenhum contracheque encontrado no intervalo informado.")
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
