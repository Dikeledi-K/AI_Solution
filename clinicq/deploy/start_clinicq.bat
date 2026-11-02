@echo off
REM Starts ClinicQ on the clinic server. Run at start-up with Task Scheduler (see LOCAL_NETWORK_SETUP.md).
REM ClinicQ only listens on this computer (127.0.0.1); tablets reach it through Caddy over HTTPS.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
