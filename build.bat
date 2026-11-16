@echo off
title Build ExFatRescue Standalone Executable
cd /d "%~dp0"
set PYTHONPATH=C:\Users\JohnCletus\AppData\Local\Programs\Python\Python314\Lib\site-packages
python -m PyInstaller --onefile --uac-admin --noconsole --add-data "paypal_qr.png;." --name "ExFatRescue" exfat_rescue.py
copy /y "dist\ExFatRescue.exe" "ExFatRescue.exe"
copy /y "dist\ExFatRescue.exe" "C:\Users\JohnCletus\Desktop\ExFatRescue.exe"
echo Build complete!
