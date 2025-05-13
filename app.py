import streamlit as st
import requests
import tempfile
import os
import re
import traceback
import json
import time
from datetime import datetime
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd
import io
from docx import Document
import tiktoken
import xml.etree.ElementTree as ET
import concurrent.futures
import langid  # Do wykrywania języka

# Stałe konfiguracyjne
SUPPORTED_FILE_TYPES = ["xml", "csv", "xls", "xlsx", "doc", "docx"]
SUPPORTED_LANGUAGES = {
    "auto": "Automatyczne wykrywanie",
    "en": "angielski", 
    "pl": "polski", 
    "de": "niemiecki", 
    "fr": "francuski", 
    "es": "hiszpański", 
    "it": "włoski"
}
CHUNK_TOKEN_LIMIT = 10000

MODEL_PRICES = {
    "openai/gpt-4o-mini": {"prompt": 0.15, "completion": 0.6},
    "mistralai/mistral-7b-instruct": {"prompt": 0.2, "completion": 0.2},
    "google/gemini-pro": {"prompt": 0.25, "completion": 0.5},
}

# Inicjalizacja stanu sesji
def init_session_state():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if "output_bytes" not in st.session_state:
        st.session_state.output_bytes = None
    if "translation_in_progress" not in st.session_state:
        st.session_state.translation_in_progress = False
    if "translation_done" not in st.session_state:
        st.session_state.translation_done = False
    if "raw_bytes" not in st.session_state:
        st.session_state.raw_bytes = None
    if "file_type" not in st.session_state:
        st.session_state.file_type = None
    if "detected_lang" not in st.session_state:
        st.session_state.detected_lang = None
    if "translated_df" not in st.session_state:
        st.session_state.translated_df = None
    if "original_df" not in st.session_state:
        st.session_state.original_df = None

@st.cache_data(ttl=3600)
def clean_invalid_xml_chars(text):
    return ''.join(
        c for c in text
        if c in ('\t', '\n', '\r') or
        (0x20 <= ord(c) <= 0xD7FF) or
        (0xE000 <= ord(c) <= 0xFFFD) or
        (0x10000 <= ord(c) <= 0x10FFFF)
    )

@st.cache_data(ttl=3600)
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

@st.cache_data(ttl=3600)
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

@st.cache_data(ttl=3600)
def is_numeric_value(value):
    """Sprawdza czy wartość jest liczbą lub kodem produktu"""
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        # Sprawdź wzorce dla kodów produktów (np. 700.KG-2)
        if re.match(r'\d{3}\.\w+-\d+', value):
            return True
        # Sprawdź czy to liczba z przecinkiem lub kropką
        if re.match(r'^\d+[,.]?\d*$', value.strip()):
            return True
    return False

@st.cache_data(ttl=3600)
def format_number_for_locale(value, target_lang):
    """Formatuje liczby zgodnie z konwencją docelowego języka"""
    try:
        # Konwertuj do float jeśli to możliwe
        if isinstance(value, str):
            # Zamień przecinki na kropki dla konwersji w Pythonie
            value = value.replace(',', '.')
            value = float(value)
            
        # Formatuj zgodnie z lokalem
        if target_lang in ['en']:  # angielski używa kropki
            return str(value).replace(',', '.')
        else:  # inne europejskie języki używają przecinka
            return str(value).replace('.', ',')
    except (ValueError, TypeError):
        # Jeśli to nie jest liczba, zwróć oryginalną wartość
        return value

@st.cache_data(ttl=3600)
def detect_language(text):
    """Wykrywa język podanego tekstu"""
    try:
        if not text or len(text.strip()) < 5:
            return None
        lang, _ = langid.classify(text)
        return lang
    except:
        return None

@st.cache_data(ttl=3600)
def detect_source_language(texts):
    """Wykrywa główny język źródłowy na podstawie próbki tekstów"""
    if not texts:
        return "auto"  # Domyślnie auto-detect
        
    # Bierz próbkę 10 najdłuższych tekstów do analizy
    sample_texts = sorted([t for t in texts if isinstance(t, str) and len(t) > 10], 
                          key=len, reverse=True)[:10]
    
    if not sample_texts:
        return "auto"
    
    # Liczniki języków
    lang_counts = {}
    
    for text in sample_texts:
        detected = detect_language(text)
        if detected:
            lang_counts[detected] = lang_counts.get(detected, 0) + 1
    
    if not lang_counts:
        return "auto"
        
    # Zwróć najczęściej wykryty język
    source_lang = max(lang_counts.items(), key=lambda x: x[1])[0]
    return source_lang

@st.cache_data(ttl=3600)
def chunk_lines(lines, model_name="gpt-4", chunk_token_limit=10000):
    # Sprawdź czy tiktoken ma wsparcie dla danego modelu
    try:
        enc = tiktoken.encoding_for_model(model_name)
    except:
        # Fallback do cl100k_base jako bezpiecznej opcji
        enc = tiktoken.get_encoding("cl100k_base")
        
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

@st.cache_data(ttl=3600)
def estimate_cost(chunks, model_name):
    # Sprawdź czy tiktoken ma wsparcie dla danego modelu
    try:
        enc = tiktoken.encoding_for_model(model_name)
    except:
        # Fallback do cl100k_base jako bezpiecznej opcji
        enc = tiktoken.get_encoding("cl100k_base")
        
    prompt_tokens = sum(len(enc.encode(line)) for _, line in sum(chunks, []))
    completion_tokens = int(prompt_tokens * 1.2)
    pricing = MODEL_PRICES.get(model_name, {"prompt": 1.0, "completion": 1.0})
    cost_prompt = prompt_tokens / 1_000_000 * pricing["prompt"]
    cost_completion = completion_tokens / 1_000_000 * pricing["completion"]
    return prompt_tokens, completion_tokens, cost_prompt + cost_completion

def retry_api_call(func, max_retries=3, initial_backoff=1):
    retries = 0
    while retries < max_retries:
        try:
            return func()
        except Exception as e:
            retries += 1
            if retries >= max_retries:
                st.error(f"Wyczerpano limit prób ({max_retries})")
                raise
            wait_time = initial_backoff * (2 ** (retries - 1))  # Exponential backoff
            st.warning(f"Próba {retries} nieudana: {e}. Ponowienie za {wait_time}s")
            time.sleep(wait_time)

def translate_chunks_with_progress(chunks, source_lang, target_lang, model, api_key):
    """Wersja funkcji translate_chunks z paskiem postępu"""
    translated_pairs = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    for i, chunk in enumerate(chunks):
        status_text.text(f"Tłumaczenie części {i + 1} z {len(chunks)}...")
        content = "\n".join(line for _, line in chunk)
        expected_count = len(chunk)
        
        # Utworzenie instrukcji z jasnym formatem wyjściowym
        if source_lang == "auto":
            prompt = (f"Translate the following text to {target_lang}. "
                     f"Keep exactly the same structure, preserving all numbers, codes and special characters. "
                     f"Return each line translated in the original order, without adding line numbers.\n\n"
                     f"Text to translate:\n{content}")
        else:
            prompt = (f"Translate the following text from {source_lang} to {target_lang}. "
                     f"Keep exactly the same structure, preserving all numbers, codes and special characters. "
                     f"Return each line translated in the original order, without adding line numbers.\n\n"
                     f"Text to translate:\n{content}")
        
        # Dodaj instrukcję systemową z jasnymi wytycznymi
        system_prompt = ("You are a precise translator. Translate exactly what is provided, "
                        "preserving all numbers, product codes, measurements and technical specifications. "
                        "Do not add, remove or change any numerical values. "
                        "Keep the original format intact.")
        
        def make_api_call():
            res = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model, 
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ]
                },
                timeout=60
            )
            res.raise_for_status()
            return res.json()
        
        try:
            result = retry_api_call(make_api_call)
            result_lines = result["choices"][0]["message"]["content"].splitlines()
            
            # Dopasuj liczbę linii w wyniku
            if len(result_lines) < expected_count:
                st.warning(f"Brakujące linie w tłumaczeniu ({len(result_lines)} zamiast {expected_count})")
                result_lines += [""] * (expected_count - len(result_lines))
            elif len(result_lines) > expected_count:
                st.warning(f"Dodatkowe linie w tłumaczeniu ({len(result_lines)} zamiast {expected_count})")
                result_lines = result_lines[:expected_count]
            
            # Utwórz pary (indeks, tłumaczenie)
            for (idx, original), translated in zip(chunk, result_lines):
                translated_pairs.append((idx, translated.strip()))
                
        except Exception as e:
            st.error(f"Błąd podczas tłumaczenia: {e}")
            # Wstaw oryginały dla nieudanych tłumaczeń
            for idx, original in chunk:
                translated_pairs.append((idx, original))
        
        # Aktualizuj pasek postępu
        progress_bar.progress((i + 1) / len(chunks))
    
    status_text.text("Tłumaczenie zakończone!")
    
    # Sortuj według oryginalnego indeksu
    translated_pairs.sort()
    return translated_pairs

@st.cache_data(ttl=3600)
def parse_csv_with_encoding_fallback(raw_bytes):
    encodings = ['utf-8', 'iso-8859-1', 'iso-8859-2', 'windows-1250']
    for enc in encodings:
        try:
            return pd.read_csv(io.BytesIO(raw_bytes), encoding=enc), enc
        except UnicodeDecodeError:
            continue
    st.error("Nie udało się rozpoznać kodowania pliku CSV")
    raise ValueError("Nieobsługiwane kodowanie pliku")

@st.cache_data(ttl=3600)
def parse_csv_with_separator_fallback(raw_bytes, encoding):
    for sep in [',', ';', '\t']:
        try:
            df = pd.read_csv(io.BytesIO(raw_bytes), encoding=encoding, sep=sep)
            if len(df.columns) > 1:  # Sprawdź czy format ma sens
                return df
        except Exception:
            continue
    
    # Ostatnia próba z automatycznym wykrywaniem separatora
    try:
        return pd.read_csv(io.BytesIO(raw_bytes), encoding=encoding, sep=None, engine='python')
    except Exception as e:
        st.error(f"Nie udało się odczytać pliku CSV: {e}")
        raise

@st.cache_data(ttl=3600)
def parse_excel_file(raw_bytes):
    """Parsowanie pliku Excel z cache'owaniem"""
    return pd.read_excel(io.BytesIO(raw_bytes))

@st.cache_data(ttl=3600)
def parse_doc_file(raw_bytes):
    """Parsowanie pliku DOC/DOCX z cache'owaniem"""
    doc = Document(io.BytesIO(raw_bytes))
    lines = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    lines.append(cell.text.strip())
    return doc, lines

def validate_translation(original_df, translated_df):
    """
    Sprawdza integralność tłumaczenia
    """
    validation_errors = []
    
    # Sprawdź czy zachowano strukturę
    if original_df.shape != translated_df.shape:
        validation_errors.append(f"Niezgodna struktura: oryginał {original_df.shape}, tłumaczenie {translated_df.shape}")
    
    # Sprawdź czy wszystkie komórki zawierające liczby zachowały swój typ
    for col in original_df.columns:
        for idx, val in original_df[col].items():
            if pd.notna(val) and is_numeric_value(val):
                trans_val = translated_df.at[idx, col]
                if not is_numeric_value(trans_val):
                    validation_errors.append(f"Utrata formatu liczbowego w komórce [{idx}, {col}]: {val} -> {trans_val}")
    
    # Sprawdź czy nie ma pustych tłumaczeń dla niepustych oryginalnych komórek
    for col in original_df.columns:
        for idx, val in original_df[col].items():
            if pd.notna(val) and str(val).strip() and not is_numeric_value(val):
                trans_val = translated_df.at[idx, col]
                if pd.isna(trans_val) or not str(trans_val).strip():
                    validation_errors.append(f"Pusta wartość tłumaczenia dla [{idx}, {col}]: {val}")
    
    return validation_errors

def translate_tabular_file(df, source_lang, target_lang, model, api_key, preserve_headers=True, maintain_numbers=True):
    # Przygotuj listę tekstów do tłumaczenia
    texts_to_translate = []
    cell_indices = []
    
    # Przygotuj do tłumaczenia tylko dane, bez nagłówków jeśli preserve_headers=True
    headers = list(df.columns)
    
    if not preserve_headers:
        for header in headers:
            texts_to_translate.append(header)
            cell_indices.append(("header", header))
        
    # Zbierz wszystkie komórki zawierające tekst
    for col in df.columns:
        for row_idx, val in df[col].items():
            if pd.notna(val) and str(val).strip():
                val_str = str(val).strip()
                # Sprawdź czy to nie jest liczba lub wartość specjalna
                if not is_numeric_value(val) or not maintain_numbers:
                    texts_to_translate.append(val_str)
                    cell_indices.append(("cell", (col, row_idx)))
    
    # Tłumacz zebrane teksty
    if texts_to_translate:
        # Wykryj język źródłowy, jeśli ustawiony na auto
        if source_lang == "auto":
            detected_lang = detect_source_language(texts_to_translate)
            st.session_state.detected_lang = detected_lang
            st.info(f"Wykryto język źródłowy: {detected_lang}")
        else:
            detected_lang = source_lang
            
        chunks = chunk_lines(texts_to_translate, model_name="gpt-4")
        translated_pairs = translate_chunks_with_progress(chunks, detected_lang, target_lang, model, api_key)
        
        # Zastosuj tłumaczenia
        translated_df = df.copy()
        
        # Indeks dla śledzenia, które tłumaczenie używamy
        trans_idx = 0
        
        # Zastosuj tłumaczenia nagłówków, jeśli potrzeba
        if not preserve_headers:
            for i, header in enumerate(headers):
                translated_df.rename(columns={header: translated_pairs[trans_idx][1]}, inplace=True)
                trans_idx += 1
                
        # Zastosuj tłumaczenia komórek
        for i, (cell_type, identifier) in enumerate(cell_indices[0 if preserve_headers else len(headers):]):
            if cell_type == "cell":
                col, row_idx = identifier
                translated_df.at[row_idx, col] = translated_pairs[trans_idx][1]
                trans_idx += 1
            
        return translated_df
    else:
        return df.copy()

def handle_file_upload():
    """Obsługa przesłania pliku z zarządzaniem stanem"""
    uploaded_file = st.file_uploader("Wgraj plik do przetłumaczenia", type=SUPPORTED_FILE_TYPES)
    
    if uploaded_file is not None:
        # Resetuj stan jeśli przesłano nowy plik
        if "file_name" not in st.session_state or st.session_state.file_name != uploaded_file.name:
            st.session_state.file_name = uploaded_file.name
            st.session_state.file_type = uploaded_file.name.split(".")[-1].lower()
            st.session_state.raw_bytes = uploaded_file.read()
            st.session_state.translation_done = False
            st.session_state.translation_in_progress = False
            st.session_state.output_bytes = None
            
        return True
    else:
        # Resetuj stan jak nie ma pliku
        if "file_name" in st.session_state:
            del st.session_state.file_name
        st.session_state.file_type = None
        st.session_state.raw_bytes = None
        st.session_state.translation_done = False
        st.session_state.translation_in_progress = False
        
        return False

def process_file():
    """Przetwarzanie przesłanego pliku"""
    file_type = st.session_state.file_type
    raw_bytes = st.session_state.raw_bytes
    
    try:
        if file_type == "xml":
            tree, root = parse_xml_with_fallback(raw_bytes)
            if not tree:
                st.error("Nie udało się odczytać pliku XML.")
                return None
            pairs = extract_xml_texts_and_paths(root)
            if not pairs:
                st.warning("Nie znaleziono tekstów do tłumaczenia w XML.")
                return None
            
            keys, lines = zip(*pairs) if pairs else ([], [])
            
            # Zapisz dane w stanie sesji
            st.session_state.xml_keys = keys
            st.session_state.xml_tree = tree
            st.session_state.xml_root = root
            
            return lines
            
        elif file_type == "csv":
            df, encoding = parse_csv_with_encoding_fallback(raw_bytes)
            df = parse_csv_with_separator_fallback(raw_bytes, encoding)
            
            # Zapisz dane w stanie sesji
            st.session_state.csv_encoding = encoding
            st.session_state.original_df = df
            
            # Przygotowanie do estymacji kosztów
            texts_to_translate = []
            
            if not st.session_state.get("preserve_headers", True):
                texts_to_translate.extend(df.columns)
                
            for col in df.columns:
                for _, val in df[col].items():
                    if pd.notna(val) and not is_numeric_value(val):
                        texts_to_translate.append(str(val))
                
            return texts_to_translate
            
        elif file_type in ["xls", "xlsx"]:
            df = parse_excel_file(raw_bytes)
            
            # Zapisz dane w stanie sesji
            st.session_state.original_df = df
            
            # Przygotowanie do estymacji kosztów
            texts_to_translate = []
            
            if not st.session_state.get("preserve_headers", True):
                texts_to_translate.extend(df.columns)
                
            for col in df.columns:
                for _, val in df[col].items():
                    if pd.notna(val) and not is_numeric_value(val):
                        texts_to_translate.append(str(val))
                
            return texts_to_translate
            
        elif file_type in ["doc", "docx"]:
            doc, lines = parse_doc_file(raw_bytes)
            
            # Zapisz dane w stanie sesji
            st.session_state.doc_object = doc
                        
            return lines
        else:
            st.error("Nieobsługiwany typ pliku.")
            return None
    
    except Exception as e:
        st.error(f"Błąd podczas przetwarzania pliku: {e}")
        return None

def save_translation_to_file(output_path, file_type):
    """Zapisuje przetłumaczony plik na dysk"""
    if file_type == "xml":
        tree = st.session_state.xml_tree
        tree.write(output_path, encoding="utf-8", xml_declaration=True)
    
    elif file_type in ["csv"]:
        translated_df = st.session_state.translated_df
        translated_df.to_csv(output_path, index=False, encoding="utf-8")
        
    elif file_type in ["xls", "xlsx"]:
        translated_df = st.session_state.translated_df
        translated_df.to_excel(output_path, index=False)
        
    elif file_type in ["doc", "docx"]:
        new_doc = st.session_state.new_doc
        new_doc.save(output_path)

def save_to_google_drive(output_path, file_type):
    """Zapisuje plik na Google Drive"""
    drive_folder_id = st.secrets.get("GOOGLE_DRIVE_FOLDER_ID")
    service_account_json = st.secrets.get("GOOGLE_DRIVE_CREDENTIALS_JSON")
    
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

def start_translation():
    """Rozpocznij proces tłumaczenia"""
    st.session_state.translation_in_progress = True
    
def handle_translation():
    """Obsługa procesu tłumaczenia"""
    file_type = st.session_state.file_type
    
    try:
        if file_type in ["csv", "xls", "xlsx"]:
            # Tłumaczenie dla plików tabelarycznych
            df = st.session_state.original_df
            source_lang = st.session_state.source_lang
            target_lang = st.session_state.target_lang
            model = st.session_state.model
            api_key = st.secrets["OPENROUTER_API_KEY"]
            preserve_headers = st.session_state.get("preserve_headers", True)
            maintain_numbers = st.session_state.get("maintain_numbers", True)
            
            translated_df = translate_tabular_file(
                df, source_lang, target_lang, model, api_key, 
                preserve_headers=preserve_headers,
                maintain_numbers=maintain_numbers
            )
            
            # Zapisz wynik w stanie sesji
            st.session_state.translated_df = translated_df
            
            with tempfile.TemporaryDirectory() as tmpdir:
                output_path = os.path.join(tmpdir, f"output.{file_type}")
                save_translation_to_file(output_path, file_type)
                    
                with open(output_path, "rb") as f:
                    st.session_state.output_bytes = f.read()
                
                # Opcjonalnie zapisz na Google Drive
                save_to_google_drive(output_path, file_type)
            
        else:
            # Tłumaczenie dla XML i dokumentów
            chunks = st.session_state.chunks
            source_lang = st.session_state.source_lang
            if source_lang == "auto" and st.session_state.detected_lang:
                source_lang = st.session_state.detected_lang
            target_lang = st.session_state.target_lang
            model = st.session_state.model
            api_key = st.secrets["OPENROUTER_API_KEY"]
            
            translated_pairs = translate_chunks_with_progress(chunks, source_lang, target_lang, model, api_key)
            
            with tempfile.TemporaryDirectory() as tmpdir:
                output_path = os.path.join(tmpdir, f"output.{file_type}")
                
                if file_type == "xml":
                    keys = st.session_state.xml_keys
                    root = st.session_state.xml_root
                    translated_map = {keys[i]: line for i, (_, line) in enumerate(translated_pairs)}
                    insert_translations_into_xml(root, translated_map)
                    st.session_state.xml_tree.write(output_path, encoding="utf-8", xml_declaration=True)
                
                elif file_type in ["doc", "docx"]:
                    doc = st.session_state.doc_object
                    new_doc = Document()
                    index = 0
                    
                    for p in doc.paragraphs:
                        if p.text.strip():
                            new_doc.add_paragraph(translated_pairs[index][1])
                            index += 1
                            
                    for table in doc.tables:
                        new_table = new_doc.add_table(rows=len(table.rows), cols=len(table.columns))
                        for i, row in enumerate(table.rows):
                            for j, cell in enumerate(row.cells):
                                if cell.text.strip():
                                    new_table.cell(i, j).text = translated_pairs[index][1]
                                    index += 1
                                    
                    st.session_state.new_doc = new_doc
                    new_doc.save(output_path)
                
                with open(output_path, "rb") as f:
                    st.session_state.output_bytes = f.read()
                
                # Opcjonalnie zapisz na Google Drive
                save_to_google_drive(output_path, file_type)
        
        st.session_state.translation_done = True
        st.session_state.translation_in_progress = False
            
    except Exception as e:
        st.error(f"Błąd podczas tłumaczenia: {e}")
        st.session_state.translation_in_progress = False
        traceback.print_exc()

def run_streamlit_app():
    # Inicjalizacja stanu sesji
    init_session_state()
    
    st.set_page_config(page_title="Tłumacz plików AI", layout="centered")
    st.title("AI Tłumacz plików CSV, XML, Excel i Word")
    
    st.markdown("""
    To narzędzie umożliwa tłumaczenie zawartości plików CSV, XML, XLS, XLSX, DOC i DOCX za pomocą wybranego modelu LLM.
    Prześlij plik, wybierz język źródłowy i docelowy oraz model.
    """)
    
    # Uwierzytelnianie
    if not st.session_state.authenticated:
        user = st.text_input("Login")
        password = st.text_input("Hasło", type="password")
        if st.button("Zaloguj"):
            if user == st.secrets.get("APP_USER") and password == st.secrets.get("APP_PASSWORD"):
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Nieprawidłowy login lub hasło")
        return
    
    # Interfejs główny pobierania pliku
    file_uploaded = handle_file_upload()
    
    if not file_uploaded:
        return
    
    # Przetwarzanie pliku i wyświetlenie opcji
    lines = process_file()
    
    if lines is None:
        return
    
    # Opcje dla CSV/Excel
    if st.session_state.file_type in ["csv", "xls", "xlsx"]:
        st.session_state.preserve_headers = st.checkbox("Zachowaj oryginalne nagłówki", value=True)
        st.session_state.maintain_numbers = st.checkbox("Zachowaj oryginalne wartości liczbowe", value=True)
    
    # Wybór języka źródłowego i docelowego
    st.session_state.source_lang = st.selectbox(
        "Język źródłowy", 
        list(SUPPORTED_LANGUAGES.keys()), 
        format_func=lambda x: f"{x} - {SUPPORTED_LANGUAGES[x]}" if x != "auto" else SUPPORTED_LANGUAGES[x],
        index=0  # Domyślnie "auto"
    )
    
    st.session_state.target_lang = st.selectbox(
        "Język docelowy", 
        [lang for lang in SUPPORTED_LANGUAGES.keys() if lang != "auto"], 
        format_func=lambda x: f"{x} - {SUPPORTED_LANGUAGES[x]}"
    )
    
    st.session_state.model = st.selectbox(
        "Wybierz model LLM (OpenRouter)", 
        list(MODEL_PRICES.keys()) + ["openai/gpt-4o", "openai/gpt-4-turbo", "anthropic/claude-3-opus"]
    )
    
    # Wykryj język, jeśli ustawiony na auto
    if st.session_state.source_lang == "auto" and lines:
        detected_lang = detect_source_language(lines)
        st.session_state.detected_lang = detected_lang
        st.info(f"Wykryto język źródłowy: {detected_lang}")
    
    # Przygotowanie chunków i estymacja kosztów
    st.session_state.chunks = chunk_lines(lines, model_name="gpt-4", chunk_token_limit=CHUNK_TOKEN_LIMIT)
    chunks = st.session_state.chunks
    prompt_tokens, completion_tokens, cost_total = estimate_cost(chunks, st.session_state.model)
    
    st.info(f"Szacunkowe zużycie tokenów: ~{prompt_tokens} (prompt) + ~{completion_tokens} (output)")
    st.info(f"Szacunkowy koszt tłumaczenia: ~${cost_total:.4f} USD")
    
    # Obsługa tłumaczenia
    if not st.session_state.translation_in_progress and not st.session_state.translation_done:
        if st.button("Przetłumacz plik"):
            start_translation()
            st.rerun()
    
    # Tłumaczenie w trakcie
    if st.session_state.translation_in_progress:
        handle_translation()
        # Unikaj rerun aby nie zresetować widoku
    
    # Wynik tłumaczenia
    if st.session_state.translation_done:
        st.success("Tłumaczenie zakończone. Plik gotowy do pobrania.")
        
        # Wyświetl przykładowe dane dla plików tabelarycznych
        if st.session_state.file_type in ["csv", "xls", "xlsx"]:
            # Walidacja rezultatu
            validation_errors = validate_translation(
                st.session_state.original_df, 
                st.session_state.translated_df
            )
            
            if validation_errors:
                st.warning("Wykryto potencjalne problemy z tłumaczeniem:")
                for error in validation_errors[:10]:  # Pokaż maksymalnie 10 błędów
                    st.write(f"- {error}")
            
            # Porównanie oryginału i tłumaczenia
            col1, col2 = st.columns(2)
            with col1:
                st.write("Przykładowe dane oryginalne:")
                st.dataframe(st.session_state.original_df.head(5))
            with col2:
                st.write("Przykładowe dane przetłumaczone:")
                st.dataframe(st.session_state.translated_df.head(5))
        
        # Przycisk do pobrania
        if st.session_state.output_bytes:
            st.download_button(
                "📁 Pobierz przetłumaczony plik", 
                data=st.session_state.output_bytes, 
                file_name=f"translated_output.{st.session_state.file_type}", 
                mime="application/octet-stream"
            )
        
        # Opcja do resetowania i rozpoczęcia nowego tłumaczenia
        if st.button("Rozpocznij nowe tłumaczenie"):
            st.session_state.translation_done = False
            st.session_state.translation_in_progress = False
            st.session_state.output_bytes = None
            st.rerun()

if __name__ == "__main__":
    run_streamlit_app()
