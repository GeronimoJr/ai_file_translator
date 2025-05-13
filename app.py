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
import docx

st.set_page_config(page_title="Tłumacz plików AI", layout="centered")
st.title("AI Tłumacz plików CSV, XML, Excel i Word")
st.markdown("""
To narzędzie umożliwia tłumaczenie zawartości plików CSV, XML, XLS, XLSX, DOC i DOCX za pomocą wybranego modelu LLM.
Prześlij plik, wybierz język docelowy oraz model.
""")

# --- Uwierzytelnianie ---
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

# --- Stan aplikacji ---
if "translated_text" not in st.session_state:
    st.session_state.translated_text = None
if "output_bytes" not in st.session_state:
    st.session_state.output_bytes = None

# --- Konfiguracja Google Drive ---
drive_folder_id = st.secrets.get("GOOGLE_DRIVE_FOLDER_ID")
service_account_json = st.secrets.get("GOOGLE_DRIVE_CREDENTIALS_JSON")

uploaded_file = st.file_uploader("Wgraj plik do przetłumaczenia", type=["xml", "csv", "xls", "xlsx", "doc", "docx"])
target_lang = st.selectbox("Język docelowy", ["en", "pl", "de", "fr", "es", "it"])
model = st.selectbox("Wybierz model LLM (OpenRouter)", [
    "openai/gpt-4o-mini",
    "openai/gpt-4o",
    "openai/gpt-4-turbo",
    "anthropic/claude-3-opus",
    "mistralai/mistral-7b-instruct",
    "google/gemini-pro"
])
api_key = st.secrets["OPENROUTER_API_KEY"]

if uploaded_file and api_key and target_lang:
    file_type = uploaded_file.name.split(".")[-1].lower()
    raw_bytes = uploaded_file.read()

    if st.button("Przetłumacz plik"):
        try:
            if file_type == "csv":
                df = pd.read_csv(io.BytesIO(raw_bytes))
                content = df.to_csv(index=False)
            elif file_type in ["xls", "xlsx"]:
                df = pd.read_excel(io.BytesIO(raw_bytes))
                content = df.to_csv(index=False)
            elif file_type == "xml":
                content = raw_bytes.decode("utf-8")
            elif file_type in ["doc", "docx"]:
                doc = docx.Document(io.BytesIO(raw_bytes))
                content = "\n".join([p.text for p in doc.paragraphs])
            else:
                st.error("Nieobsługiwany typ pliku.")
                st.stop()

            prompt = f"Przetłumacz poniższy tekst na język {target_lang}. Zwróć sam przetłumaczony tekst.\n\n{content[:2000]}"

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            data = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "Jesteś pomocnym tłumaczem tekstów."},
                    {"role": "user", "content": prompt}
                ]
            }

            with st.spinner("Tłumaczenie zawartości..."):
                res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data)
                translated = res.json()["choices"][0]["message"]["content"]

            with tempfile.TemporaryDirectory() as tmpdirname:
                output_path = os.path.join(tmpdirname, f"output.{file_type}")
                if file_type in ["csv", "xls", "xlsx"]:
                    df_translated = pd.read_csv(io.StringIO(translated))
                    if file_type == "csv":
                        df_translated.to_csv(output_path, index=False)
                    else:
                        df_translated.to_excel(output_path, index=False)
                elif file_type == "xml":
                    with open(output_path, "w", encoding="utf-8") as f:
                        f.write(translated)
                elif file_type in ["doc", "docx"]:
                    new_doc = docx.Document()
                    for line in translated.splitlines():
                        new_doc.add_paragraph(line)
                    new_doc.save(output_path)

                with open(output_path, "rb") as f:
                    st.session_state.output_bytes = f.read()

                now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                result_filename = f"output_{now}.{file_type}"

                if drive_folder_id and service_account_json:
                    creds_dict = json.loads(service_account_json)
                    scope = ["https://www.googleapis.com/auth/drive"]
                    credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
                    gauth = GoogleAuth()
                    gauth.credentials = credentials
                    drive = GoogleDrive(gauth)

                    result_file = drive.CreateFile({"title": result_filename, "parents": [{"id": drive_folder_id}]})
                    result_file.SetContentFile(output_path)
                    result_file.Upload()

                    st.success("Plik przetłumaczenia zapisany na Google Drive ✅")

        except Exception as e:
            st.error("Błąd podczas tłumaczenia lub zapisu:")
            st.exception(traceback.format_exc())

if st.session_state.output_bytes:
    st.download_button(
        label="📁 Pobierz przetłumaczony plik",
        data=st.session_state.output_bytes,
        file_name=f"translated.{file_type}",
        mime="application/octet-stream"
    )
