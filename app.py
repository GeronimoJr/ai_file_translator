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
import ast
import pandas as pd
import io

st.set_page_config(page_title="Edytor XML/CSV z AI", layout="centered")
st.title("Edytor AI plików XML i CSV")
st.markdown("""
To narzędzie umożliwia modyfikację plików XML i CSV przy użyciu sztucznej inteligencji.
Prześlij plik i wpisz polecenie w języku naturalnym, np.: _"Dodaj kolumnę Czas dostawy zależnie od dostępności"_.
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
if "generated_code" not in st.session_state:
    st.session_state.generated_code = ""
if "output_bytes" not in st.session_state:
    st.session_state.output_bytes = None

# --- Konfiguracja Google Drive z Service Account ---
drive_folder_id = st.secrets.get("GOOGLE_DRIVE_FOLDER_ID")
service_account_json = st.secrets.get("GOOGLE_DRIVE_CREDENTIALS_JSON")

uploaded_file = st.file_uploader("Wgraj plik XML lub CSV", type=["xml", "csv"])
instruction = st.text_area("Instrukcja modyfikacji (w języku naturalnym)")

model = st.selectbox("Wybierz model LLM (OpenRouter)", [
    "openai/gpt-4o-mini",
    "openai/gpt-4o",
    "openai/gpt-4-turbo",
    "anthropic/claude-3-opus",
    "mistralai/mistral-7b-instruct",
    "google/gemini-pro"
])

api_key = st.secrets["OPENROUTER_API_KEY"]

if uploaded_file and instruction and api_key:
    raw_bytes = uploaded_file.read()
    encoding_declared = re.search(br'<\?xml[^>]*encoding=["\']([^"\']+)["\']', raw_bytes)
    encodings_to_try = [encoding_declared.group(1).decode('ascii')] if encoding_declared else []
    encodings_to_try += ["utf-8", "iso-8859-2", "windows-1250", "utf-16"]

    for enc in encodings_to_try:
        try:
            file_contents = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        st.error("Nie udało się odczytać pliku – nieznane kodowanie.")
        st.stop()

    file_type = uploaded_file.name.split(".")[-1].lower()

    if st.button("Wygeneruj kod Python"):
        prompt = f"""
Jesteś pomocnym asystentem, który generuje kod Python do modyfikacji plików typu {file_type.upper()}.
Użytkownik przesłał plik wejściowy. Kod powinien:
1. Wczytać plik z podanej ścieżki `input_path`
2. Zmodyfikować dane zgodnie z poniższą instrukcją
3. Zapisz wynik jako `output_path`

Dane wejściowe (fragment):
{file_contents[:1000]}

Instrukcja użytkownika:
{instruction}

Jeśli to plik CSV, użyj biblioteki pandas. Jeśli to XML, użyj xml.etree.ElementTree.

Wygeneruj kompletny kod, który:
- Otwiera plik z input_path
- Modyfikuje dane
- Zapisuje wynik do output_path

Nie dodawaj żadnych opisów ani komentarzy. Zwróć wyłącznie czysty kod Python.
        """

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": model,
            "messages": [
                {"role": "system", "content": "Jesteś asystentem kodującym w Pythonie."},
                {"role": "user", "content": prompt}
            ]
        }

        with st.spinner("Generowanie kodu Python..."):
            res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data)
            code = res.json()["choices"][0]["message"]["content"]

            code = re.sub(r"```(?:python)?\n", "", code)
            code = code.replace("```", "")
            code = re.sub(r"^\s*#.*$", "", code, flags=re.MULTILINE)
            code = re.sub(r"^\s*(print\(.*\)|if __name__ == .__main__.:.*)$", "", code, flags=re.MULTILINE)
            code = re.sub(r"(?i)^.*(oto kod|przykład|python).*$", "", code, flags=re.MULTILINE)

            def sanitize_code(code):
                lines = code.strip().splitlines()
                while lines:
                    try:
                        ast.parse("\n".join(lines))
                        break
                    except SyntaxError:
                        lines.pop()
                return "\n".join(lines)

            code = sanitize_code(code)

            st.session_state.generated_code = code.strip()
            st.session_state.output_bytes = None

    if st.session_state.generated_code:
        st.subheader("Wygenerowany kod:")
        st.code(st.session_state.generated_code, language="python")

        if st.button("Wykonaj kod i zapisz wynik"):
            with tempfile.TemporaryDirectory() as tmpdirname:
                input_path = os.path.join(tmpdirname, f"input.{file_type}")
                output_path = os.path.join(tmpdirname, f"output.{file_type}")
                if file_type == "xml":
                    with open(input_path, "wb") as f:
                        f.write(raw_bytes)
                else:
                    with open(input_path, "w", encoding="utf-8") as f:
                        f.write(file_contents)

                code = st.session_state.generated_code
                code = re.sub(r"input_path\s*=.*", "", code)
                code = re.sub(r"output_path\s*=.*", "", code)

                st.text("\n[DEBUG] Wykonywany kod:")
                st.code(code, language="python")

                try:
                    exec_globals = {
                        "__builtins__": __builtins__,
                        "input_path": input_path,
                        "output_path": output_path
                    }
                    exec(code, exec_globals)

                    st.text("[DEBUG] Zawartość katalogu tymczasowego:")
                    st.text("\n".join(os.listdir(tmpdirname)))

                    if os.path.exists(output_path):
                        with open(output_path, "rb") as f:
                            st.session_state.output_bytes = f.read()

                        now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                        log_filename = f"history_{now}.txt"
                        result_filename = f"output_{now}.{file_type}"

                        with open(log_filename, "w", encoding="utf-8") as log:
                            log.write(f"INSTRUCTION:\n{instruction}\n\nCODE:\n{st.session_state.generated_code}")

                        if drive_folder_id and service_account_json:
                            creds_dict = json.loads(service_account_json)
                            scope = ["https://www.googleapis.com/auth/drive"]
                            credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
                            gauth = GoogleAuth()
                            gauth.credentials = credentials
                            drive = GoogleDrive(gauth)

                            history_file = drive.CreateFile({"title": log_filename, "parents": [{"id": drive_folder_id}]})
                            history_file.SetContentFile(log_filename)
                            history_file.Upload()

                            result_file = drive.CreateFile({"title": result_filename, "parents": [{"id": drive_folder_id}]})
                            result_file.SetContentFile(output_path)
                            result_file.Upload()

                            st.success("Pliki zapisane na Twoim Google Drive ✅")
                    else:
                        st.error("Nie znaleziono pliku wynikowego.")
                except Exception as e:
                    st.error("Błąd wykonania kodu:")
                    st.exception(traceback.format_exc())

    if st.session_state.output_bytes:
        st.download_button(
            label="📁 Pobierz zmodyfikowany plik",
            data=st.session_state.output_bytes,
            file_name=f"output.{file_type}",
            mime="text/plain"
        )
