@echo off
title Site Editor
cd /d "%~dp0"
echo Starting the site editor...
python _editor\server.py
if errorlevel 1 pause
