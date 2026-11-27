@echo off
setlocal
if "%NLM_HOME%"=="" set "NLM_HOME=%USERPROFILE%\.nlm-pipeline"
if not exist "%NLM_HOME%\.venv\Scripts\python.exe" (
  echo X Not installed. Run: powershell -ExecutionPolicy Bypass -File "%~dp0install.ps1"
  exit /b 9
)
"%NLM_HOME%\.venv\Scripts\python.exe" -X utf8 "%~dp0scripts\nlm.py" %*
