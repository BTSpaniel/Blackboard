@echo off
setlocal
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
echo Starting Blackboard at http://127.0.0.1:8780/
start "Blackboard Browser" /min cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:8780/ && exit"
python -m uvicorn blackboard.main:app --host 127.0.0.1 --port 8780
endlocal
