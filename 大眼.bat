@echo off
rem Launch desktop.py with windowless pythonw (avoid black console).
rem Probe PATH first, then common install locations (no hardcoded username), fallback to pyw launcher.
set "PYW="
for %%P in (pythonw.exe) do if not defined PYW set "PYW=%%~$PATH:P"
if not defined PYW if exist "%LocalAppData%\Programs\Python\Python313\pythonw.exe" set "PYW=%LocalAppData%\Programs\Python\Python313\pythonw.exe"
if not defined PYW if exist "%LocalAppData%\Programs\Python\Python311\pythonw.exe" set "PYW=%LocalAppData%\Programs\Python\Python311\pythonw.exe"
if defined PYW (start "" "%PYW%" "%~dp0desktop.py") else (start "" pyw "%~dp0desktop.py")