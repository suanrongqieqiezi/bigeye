@echo off
set PYW=C:\Users\Hugh\AppData\Local\Programs\Python\Python313\pythonw.exe
if exist "%PYW%" (start "" "%PYW%" "%~dp0desktop.py") else (start "" py "%~dp0desktop.py")
