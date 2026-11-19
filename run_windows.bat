@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul && (python jira_toolkit.py & goto :eof)
where py >nul 2>nul && (py -3 jira_toolkit.py & goto :eof)
echo Python 3 не найден. Установите Python 3.8+ с python.org или из корпоративного портала.
pause
