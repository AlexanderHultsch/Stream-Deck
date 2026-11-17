@echo off
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 goto usepy
where python >nul 2>nul
if not errorlevel 1 goto usepython
echo Python 3 not found.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
goto wait

:usepy
py -3 server.py %*
goto stopped

:usepython
python server.py %*

:stopped
echo.
echo The Stream Deck server has stopped.

:wait
pause
