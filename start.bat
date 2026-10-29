@echo off
REM One-step start for Windows: creates a virtual environment, installs dependencies, runs LearnDeck.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist .venv\Scripts\python.exe (
  echo Creating a virtual environment...
  %PY% -m venv .venv || (echo LearnDeck needs Python 3.9 or newer from https://www.python.org/downloads/ & pause & exit /b 1)
)
if not exist .venv\.installed (
  echo Installing dependencies...
  .venv\Scripts\python -m pip install --upgrade pip >nul
  .venv\Scripts\python -m pip install -r requirements.txt || (pause & exit /b 1)
  type nul > .venv\.installed
)
if not exist .env copy .env.example .env >nul
.venv\Scripts\python run.py %*
