set HF_ENDPOINT=https://hf-mirror.com
start "ºó¶Ë" cmd /k "python api_server.py"
timeout /t 5 /nobreak >nul
start "Ç°¶Ë" cmd /k "streamlit run app_ui.py"
