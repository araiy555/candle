@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py
echo 最新版を確認しています...
git pull --ff-only
if not exist config.toml copy config.example.toml config.toml >nul
%PY% -m candle launch
pause
