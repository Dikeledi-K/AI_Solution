@echo off
REM Daily encrypted backup of the ClinicQ database. Schedule with Task Scheduler (see LOCAL_NETWORK_SETUP.md).
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
if not exist backups mkdir backups
python -m core.backup create >> backups\backup_log.txt 2>&1
