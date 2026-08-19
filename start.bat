@echo off
chcp 65001 >nul
echo ================================
echo   大眼X
echo ================================
echo.

set "WORKDIR=%~dp0"
set "PORT=9890"
set "LOGFILE=%WORKDIR%\_server_output.log"

echo 启动端口: %PORT%
echo 工作目录: %WORKDIR%
echo.

cd /d "%WORKDIR%"

echo 正在启动大眼X...
start /B "" py server.py --port %PORT% > "%LOGFILE%" 2>&1

echo.
echo 大眼X已启动！ http://127.0.0.1:%PORT%
echo 按任意键查看启动日志...
pause >nul
type "%LOGFILE%"
pause
