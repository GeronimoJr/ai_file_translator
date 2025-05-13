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

st.set_page_config(page_title="Tłumacz plików AI", layout="centered")
st.title("AI Tłumacz plików CSV, XML, Excel i Word")
st.markdown("""
To narzędzie umożliwia tłumaczenie zawartości plików CSV, XML, XLS, XLSX, DOC, DOCX za pomocą wybranego modelu LLM.
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

if uploaded_file and target_lang and api_key:
    file_type = uploaded_file.name.split(".")[-1].lower()
    try:
        if file_type in ["csv"]:
            df = pd.read_csv(uploaded_file)
            text_to_translate = df.to_csv(index=False)
        elif file_type in ["xls", "xlsx"]:
            df = pd.read_excel(uploaded_file)
            text_to_translate = df.to_csv(index=False)
        elif file_type == "xml":
            text_to_translate = uploaded_file.read().decode("utf-8")
        elif file_type in ["doc", "docx"]:
            import docx
            doc = docx.Document(uploaded_file)
            text_to_translate = "\n".join([p.text for p in doc.paragraphs])
        else:
            st.error("Nieobsługiwany typ pliku")
            st.stop()

        prompt = f"Przetłumacz poniższy tekst na język {target_lang}. Zwróć sam przetłumaczony tekst.\n\n{text_to_translate[:2000]}"

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
            st.session_state.translated_text = translated

    except Exception as e:
        st.error("Błąd podczas tłumaczenia:")
        st.exception(traceback.format_exc())

if st.session_state.translated_text:
    st.subheader("Przetłumaczony tekst")
    st.text_area("Wynik tłumaczenia", st.session_state.translated_text, height=300)

    if st.button("Zapisz tłumaczenie na Google Drive"):
        now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        result_filename = f"translation_{now}.txt"

        with open(result_filename, "w", encoding="utf-8") as f:
            f.write(st.session_state.translated_text)

        if drive_folder_id and service_account_json:
            creds_dict = json.loads(service_account_json)
            scope = ["https://www.googleapis.com/auth/drive"]
            credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            gauth = GoogleAuth()
            gauth.credentials = credentials
            drive = GoogleDrive(gauth)

            result_file = drive.CreateFile({"title": result_filename, "parents": [{"id": drive_folder_id}]})
            result_file.SetContentFile(result_filename)
            result_file.Upload()

            st.success("Plik przetłumaczenia zapisany na Google Drive ✅")

    st.download_button(
        label="📁 Pobierz tłumaczenie",
        data=st.session_state.translated_text.encode("utf-8"),
        file_name="translated.txt",
        mime="text/plain"
    )
