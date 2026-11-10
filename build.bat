@echo off
title Build ExFatRescue Standalone Executable
echo Building ExFatRescue.exe with embedded UAC administrator manifest...
cd /d "%~dp0"
"C:\Users\JohnCletus\AppData\Local\Programs\Python\Python314\Scripts\pyinstaller.exe" --onefile --uac-admin --noconsole --name "ExFatRescue" exfat_rescue.py
echo.
echo Build complete! Executable is in: %~dp0dist\ExFatRescue.exe
pause
