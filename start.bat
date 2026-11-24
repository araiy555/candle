@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py
if not exist config.toml copy config.example.toml config.toml >nul
if "%KABU_API_PASSWORD%"=="" set /p KABU_API_PASSWORD=APIパスワードを入力して Enter: 
%PY% -m candle check
if errorlevel 1 (
  echo.
  pause
  exit /b 1
)
start "" http://localhost:8765
%PY% -m candle watch
pause
