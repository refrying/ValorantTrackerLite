@echo off
chcp 65001 >nul
py -m pip install -r "%~dp0requirements.txt"
py "%~dp0tracker.py" %*
pause
