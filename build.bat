@echo off
setlocal

py -m pip install -r requirements.txt
py -m PyInstaller --noconfirm --clean --onefile --windowed --noupx --icon=NONE --name SnipClicker app.py

echo.
echo Built executable:
echo %CD%\dist\SnipClicker.exe
