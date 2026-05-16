@echo off
chcp 65001 >nul
echo ============================================
echo  Carbon Neutrality Extractor - Install
echo ============================================
echo.

py -m pip install --upgrade pip
py -m pip install -r requirements.txt

echo.
echo Install complete!
echo.
echo Next steps:
echo   1. Copy .env.example to .env
echo   2. Set GEMINI_API_KEY in .env
echo   3. Run: py main.py [PDF path]
echo.
pause
