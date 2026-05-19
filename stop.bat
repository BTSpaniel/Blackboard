@echo off
echo Stopping Blackboard (uvicorn on port 8780)...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8780" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F 2>nul
)
echo Done.
