import streamlit as st
import requests
import tempfile
import os
import re
import traceback
import json
from datetime import datetime
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd
import io
from docx import Document
import tiktoken
import xml.etree.ElementTree as ET

def clean_invalid_xml_chars(text):
    return ''.join(
        c for c in text
        if c in ('\t', '\n', '\r') or
        (0x20 <= ord(c) <= 0xD7FF) or
        (0xE000 <= ord(c) <= 0xFFFD) or
        (0x10000 <= ord(c) <= 0x10FFFF)
    )

def parse_xml_with_fallback(raw_bytes):
    match = re.search(br'<\?xml[^>]*encoding=["\']([^"\']+)["\']', raw_bytes)
    declared_enc = match.group(1).decode('ascii').lower() if match else None
    candidates = [declared_enc] if declared_enc else []
    candidates += ['utf-16', 'utf-8', 'iso-8859-2', 'windows-1250']

    for enc in candidates:
        try:
            decoded = raw_bytes.decode(enc)
            cleaned = clean_invalid_xml_chars(decoded)
            tree = ET.ElementTree(ET.fromstring(cleaned))
            return tree, tree.getroot()
        except Exception:
            continue

    return None, None

def extract_xml_texts_and_paths(elem, path=""):
    texts = []
    if elem.text and elem.text.strip():
        texts.append((f"{path}/text", elem.text.strip()))
    for k, v in elem.attrib.items():
        texts.append((f"{path}/@{k}", v))
    for i, child in enumerate(elem):
        child_path = f"{path}/{child.tag}[{i}]"
        texts.extend(extract_xml_texts_and_paths(child, child_path))
    return texts

def insert_translations_into_xml(elem, translations, path=""):
    if elem.text and elem.text.strip():
        key = f"{path}/text"
        if key in translations:
            elem.text = translations[key]
    for k in elem.attrib:
        key = f"{path}/@{k}"
        if key in translations:
            elem.attrib[k] = translations[key]
    for i, child in enumerate(elem):
        child_path = f"{path}/{child.tag}[{i}]"
        insert_translations_into_xml(child, translations, child_path)

def chunk_lines(lines, model_name="gpt-4", chunk_token_limit=10000):
    enc = tiktoken.encoding_for_model(model_name)
    chunks, current_chunk, current_tokens = [], [], 0
    for i, line in enumerate(lines):
        token_len = len(enc.encode(line))
        if current_tokens + token_len > chunk_token_limit:
            chunks.append(current_chunk)
            current_chunk, current_tokens = [], 0
        current_chunk.append((i, line))
        current_tokens += token_len
    if current_chunk:
        chunks.append(current_chunk)
    return chunks

def estimate_cost(chunks, model_name):
    enc = tiktoken.encoding_for_model("gpt-4")
    prompt_tokens = sum(len(enc.encode(line)) for _, line in sum(chunks, []))
    completion_tokens = int(prompt_tokens * 1.2)
    pricing = MODEL_PRICES.get(model_name, {"prompt": 1.0, "completion": 1.0})
    cost_prompt = prompt_tokens / 1_000_000 * pricing["prompt"]
    cost_completion = completion_tokens / 1_000_000 * pricing["completion"]
    return prompt_tokens, completion_tokens, cost_prompt + cost_completion

def translate_chunks(chunks, target_lang, model, api_key):
    translated_pairs = []
    for i, chunk in enumerate(chunks):
        with st.spinner(f"Tłumaczenie części {i + 1} z {len(chunks)}..."):
            content = "\n".join(line for _, line in chunk)
            expected_count = len(chunk)
            prompt = f"Przetłumacz na język {target_lang}. Zwróć każdą linię w oryginalnej kolejności, bez numeracji.\n\n{content}"
            res = requests.post("https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [
                    {"role": "system", "content": "Tłumacz precyzyjnie bez zmiany formatu."},
                    {"role": "user", "content": prompt}
                ]})
            result_lines = res.json()["choices"][0]["message"]["content"].splitlines()
            if len(result_lines) < expected_count:
                result_lines += [""] * (expected_count - len(result_lines))
            elif len(result_lines) > expected_count:
                result_lines = result_lines[:expected_count]
            for (idx, _), translated in zip(chunk, result_lines):
                translated_pairs.append((idx, translated.strip()))
    translated_pairs.sort()
    return translated_pairs

def parse_tabular_file(data, read_fn):
    df = read_fn(io.BytesIO(data))
    lines, indices = [], []
    for col in df.columns:
        for row_idx, val in df[col].items():
            val_str = str(val).strip()
            if val_str:
                lines.append(val_str)
                indices.append((col, row_idx))
    return df, lines, indices

st.set_page_config(page_title="Tłumacz plików AI", layout="centered")
st.title("AI Tłumacz plików CSV, XML, Excel i Word")

st.markdown("""
To narzędzie umożliwa tłumaczenie zawartości plików CSV, XML, XLS, XLSX, DOC i DOCX za pomocą wybranego modelu LLM.
Prześlij plik, wybierz język docelowy oraz model.
""")

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    user = st.text_input("Login")
    password = st.text_input("Hasło", type="password")
    if st.button("Zaloguj"):
        if user == st.secrets.get("APP_USER") and password == st.secrets.get("APP_PASSWORD"):
            st.session_state.authenticated = True
        else:
            st.error("Nieprawidłowy login lub hasło")
    st.stop()

drive_folder_id = st.secrets.get("GOOGLE_DRIVE_FOLDER_ID")
service_account_json = st.secrets.get("GOOGLE_DRIVE_CREDENTIALS_JSON")
api_key = st.secrets["OPENROUTER_API_KEY"]

MODEL_PRICES = {
    "openai/gpt-4o-mini": {"prompt": 0.15, "completion": 0.6},
    "mistralai/mistral-7b-instruct": {"prompt": 0.2, "completion": 0.2},
    "google/gemini-pro": {"prompt": 0.25, "completion": 0.5},
}

uploaded_file = st.file_uploader("Wgraj plik do przetłumaczenia", type=["xml", "csv", "xls", "xlsx", "doc", "docx"])
target_lang = st.selectbox("Język docelowy", ["en", "pl", "de", "fr", "es", "it"])
model = st.selectbox("Wybierz model LLM (OpenRouter)", list(MODEL_PRICES.keys()) + ["openai/gpt-4o", "openai/gpt-4-turbo", "anthropic/claude-3-opus"])

if uploaded_file:
    file_type = uploaded_file.name.split(".")[-1].lower()
    raw_bytes = uploaded_file.read()

    try:
        if file_type == "xml":
            tree, root = parse_xml_with_fallback(raw_bytes)
            if not tree:
                st.error("Nie udało się odczytać pliku XML.")
                st.stop()
            pairs = extract_xml_texts_and_paths(root)
            if not pairs:
                st.warning("Nie znaleziono tekstów do tłumaczenia w XML.")
            keys, lines = zip(*pairs) if pairs else ([], [])
        elif file_type == "csv":
            df, lines, cell_indices = parse_tabular_file(raw_bytes, lambda f: pd.read_csv(f, encoding="utf-8"))
        elif file_type in ["xls", "xlsx"]:
            df, lines, cell_indices = parse_tabular_file(raw_bytes, pd.read_excel)
        elif file_type in ["doc", "docx"]:
            doc = Document(io.BytesIO(raw_bytes))
            lines = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        if cell.text.strip():
                            lines.append(cell.text.strip())
        else:
            st.error("Nieobsługiwany typ pliku.")
            st.stop()

        chunks = chunk_lines(lines, model_name="gpt-4")
        prompt_tokens, completion_tokens, cost_total = estimate_cost(chunks, model)
        st.info(f"Szacunkowe zużycie tokenów: ~{prompt_tokens} (prompt) + ~{completion_tokens} (output)")
        st.info(f"Szacunkowy koszt tłumaczenia: ~${cost_total:.4f} USD")

        if st.button("Przetłumacz plik"):
            translated_pairs = translate_chunks(chunks, target_lang, model, api_key)
            if file_type in ['csv', 'xls', 'xlsx']:
                if len(translated_pairs) != len(cell_indices):
                    st.error(f'Liczba przetłumaczonych linii ({len(translated_pairs)}) nie zgadza się z liczbą danych wejściowych ({len(cell_indices)}).')
                    st.stop()

            with tempfile.TemporaryDirectory() as tmpdir:
                output_path = os.path.join(tmpdir, f"output.{file_type}")

                if file_type == "xml":
                    translated_map = {keys[i]: line for i, (_, line) in enumerate(translated_pairs)}
                    insert_translations_into_xml(root, translated_map)
                    tree.write(output_path, encoding="utf-8", xml_declaration=True)
                elif file_type in ["csv", "xls", "xlsx"]:
                    translated_df = df.copy()
                    for (idx, (col, row)) in enumerate(cell_indices):
                        translated_df.at[row, col] = translated_pairs[idx][1]
                    if file_type == "csv":
                        translated_df.to_csv(output_path, index=False)
                    else:
                        translated_df.to_excel(output_path, index=False)
                elif file_type in ["doc", "docx"]:
                    new_doc = Document()
                    index = 0
                    for p in doc.paragraphs:
                        if p.text.strip():
                            new_doc.add_paragraph(translated_pairs[index][1])
                            index += 1
                    for table in doc.tables:
                        for row in table.rows:
                            for cell in row.cells:
                                if cell.text.strip():
                                    cell.text = translated_pairs[index][1]
                                    index += 1
                    new_doc.save(output_path)

                with open(output_path, "rb") as f:
                    st.session_state.output_bytes = f.read()

                if drive_folder_id and service_account_json:
                    creds_dict = json.loads(service_account_json)
                    scope = ["https://www.googleapis.com/auth/drive"]
                    credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
                    gauth = GoogleAuth()
                    gauth.credentials = credentials
                    drive = GoogleDrive(gauth)
                    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                    result_filename = f"translated_output.{now}.{file_type}"
                    result_file = drive.CreateFile({"title": result_filename, "parents": [{"id": drive_folder_id}]})
                    result_file.SetContentFile(output_path)
                    result_file.Upload()
                    st.success("Plik zapisany na Twoim Google Drive ✅")

                st.success("Tłumaczenie zakończone. Plik gotowy do pobrania.")

    except Exception:
        st.error("Błąd podczas przetwarzania:")
        st.exception(traceback.format_exc())

if st.session_state.get("output_bytes"):
    st.download_button("📁 Pobierz przetłumaczony plik", data=st.session_state.output_bytes, file_name=f"translated_output.{file_type}", mime="application/octet-stream")
