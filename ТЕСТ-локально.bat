@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
echo === Proverka soobschestva VK (bez otpravki) ===
python vk_check.py --verbose --dry-run
echo.
echo === Gotovo. Najmite lyubuyu klavishu ===
pause >nul
