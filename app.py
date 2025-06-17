#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web‑app Streamlit – Extração dinâmica de contracheques com Adobe PDF Extract
e OCR fallback (pdfplumber + Tesseract)
"""

import re, io, json, time, uuid, requests, pdfplumber, pandas as pd, streamlit as st, pytesseract
from PIL import Image
from functools import lru_cache

# --------------------------------------------------------------------
# ---------- CONFIGURAÇÕES DA ADOBE (substitua pelos seus dados) -----
# --------------------------------------------------------------------
CLIENT_ID        = "b9cf3786302d45c2803158771beea463"
CLIENT_SECRET    = "p8e-dJzha1EVFGaVN_F567J3fAG9Z6rSQLXj"
ORG_ID           = "C63A22566851828C0A495C2F@AdobeOrg"
SCOPES           = "openid,AdobeID,DCAPI"
TOKEN_URL        = "https://ims-na1.adobelogin.com/ims/token/v3"
EXTRACT_URL      = "https://pdf-services.adobe.io/operation/extract"

# --------------------------------------------------------------------
# ---------- TOKEN – obtido e armazenado em cache --------------------
# --------------------------------------------------------------------
@lru_cache(maxsize=1)
def _cached_token():
    """
    Faz a requisição ao IMS, devolvendo (token, expiração_epoch).
    O resultado é armazenado em cache pelo lru_cache.
    """
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
        "scope": SCOPES,
    }
    r = requests.post(TOKEN_URL, data=data, timeout=30)
    r.raise_for_status()
    resp = r.json()
    return resp["access_token"], time.time() + resp["expires_in"] - 60  # 1 min de folga

def get_access_token():
    token, exp = _cached_token()
    if time.time() > exp:                # expirou → limpa cache e refaz
        _cached_token.cache_clear()
        token, exp = _cached_token()
    return token

# --------------------------------------------------------------------
# ---------- Adobe Extract PDF → texto por página --------------------
# --------------------------------------------------------------------
def extract_pdf_adobe(file_bytes):
    """
    Envia o PDF para o Adobe PDF Extract e devolve lista de textos (1 por página).
    """
    token = get_access_token()
    boundary = uuid.uuid4().hex
    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-key": CLIENT_ID,
        "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }

    # Multipart manual (requests faz isso automaticamente, mas precisamos
    # controlar o cabeçalho Content-Type do campo 'options')
    files = {
        "file": ("document.pdf", file_bytes, "application/pdf"),
        "options": ("options", json.dumps({"elements": ["text"]}), "application/json"),
    }
    r = requests.post(EXTRACT_URL, headers=headers, files=files, timeout=120)
    r.raise_for_status()
    data = r.json()

    # Organiza texto por página
    pages = {}
    for elem in data.get("elements", []):
        page = elem["Page"]
        pages.setdefault(page, []).append(elem["Text"])

    # Garante ordem
    textos = ["\n".join(pages[p]) for p in sorted(pages)]
    return textos

# --------------------------------------------------------------------
# ---------- Expressões regulares (mantidas) -------------------------
# --------------------------------------------------------------------
re_ref  = re.compile(r"Refer[eê]ncia[:\s]+([A-ZÇ]+)\/(\d{4})", re.I)
re_fgts = re.compile(r"BASE\s+CALC\.\s+FGTS\s+([\d\.,]+)", re.I)

# ---------- Funções utilitárias (mantidas) --------------------------
def normalizar_valor(txt):
    txt = txt.strip()
    if not txt or txt in {"-", "0,00"}:
        return 0.0, True
    try:
        return float(txt.replace(".", "").replace(",", ".")), True
    except ValueError:
        return 0.0, False

def extrair_recibo_texto(texto):
    avisos, linhas = [], texto.splitlines()

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

# --------------------------------------------------------------------
# ---------- Processamento principal --------------------------------
# --------------------------------------------------------------------
def processar_pdf(file_bytes, pagina_ini, pagina_fim):
    # 1) Tenta via Adobe
    try:
        textos = extract_pdf_adobe(file_bytes)
        st.info("✅ Texto extraído via Adobe PDF Extract.")
    except Exception as e:
        st.warning(f"⚠️ Falha no Adobe Extract ({e}). Usando OCR/pdfplumber.")
        textos = []

    registros, rubricas, avisos_totais = [], set(), []
    if textos:
        total_pag = len(textos)
        pagina_ini = max(1, pagina_ini)
        pagina_fim = min(total_pag, pagina_fim)
        for idx in range(pagina_ini-1, pagina_fim):
            resultado = extrair_recibo_texto(textos[idx])
            if resultado:
                mes_ano, provs, fgts, avisos = resultado
                if any(r["Mês/Ano"] == mes_ano for r in registros):
                    continue
                rubricas.update(provs.keys())
                registros.append({"Mês/Ano": mes_ano,
                                  "Proventos": provs,
                                  "Base FGTS": fgts})
                avisos_totais.extend(avisos)

    # 2) Fallback OCR/pdfplumber se nada encontrado
    if not registros:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            total_pag = len(pdf.pages)
            pagina_ini = max(1, pagina_ini)
            pagina_fim = min(total_pag, pagina_fim)
            for idx in range(pagina_ini-1, pagina_fim):
                page = pdf.pages[idx]
                # OCR se não houver texto
                texto = page.extract_text() or pytesseract.image_to_string(Image.open(io.BytesIO(page.to_image(resolution=300).original)))
                resultado = extrair_recibo_texto(texto)
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

# --------------------------------------------------------------------
# ---------- INTERFACE STREAMLIT -------------------------------------
# --------------------------------------------------------------------
st.set_page_config(page_title="Leitor de Contracheques", layout="wide")
st.title("📑 Extrator de Contracheques – Adobe PDF Extract + OCR")

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
