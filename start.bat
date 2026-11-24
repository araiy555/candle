@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py
if not exist config.toml copy config.example.toml config.toml >nul
echo.
echo  candle - モードを選んでください
echo.
echo   1  通知のみ（売買しない）
echo   2  テスト（仮想売買。実際の注文は出さない）
echo   3  本番（実際に発注する）
echo   4  発注テスト（検証用環境に注文の形式を送って確かめる）
echo.
set MODE=
set /p MODE=番号を入力して Enter: 
if "%MODE%"=="4" (
  %PY% -m candle order-check
  pause
  exit /b
)
set CMD=
if "%MODE%"=="1" set CMD=watch
if "%MODE%"=="2" set CMD=paper
if "%MODE%"=="3" set CMD=live
if "%CMD%"=="" (
  echo 1から4の番号を入力してください。
  pause
  exit /b 1
)
if "%KABU_API_PASSWORD%"=="" set /p KABU_API_PASSWORD=本番用のAPIパスワードを入力して Enter: 
%PY% -m candle check
if errorlevel 1 (
  pause
  exit /b 1
)
start "" http://localhost:8765
%PY% -m candle %CMD%
pause
