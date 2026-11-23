@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist config.toml copy config.example.toml config.toml >nul
if "%KABU_API_PASSWORD%"=="" set /p KABU_API_PASSWORD=APIパスワードを入力して Enter: 
python -m candle check
if errorlevel 1 (
  echo.
  echo 接続できませんでした。kabuステーションにログインしているか確認してください。
  pause
  exit /b 1
)
start "" http://localhost:8765
python -m candle paper
pause
