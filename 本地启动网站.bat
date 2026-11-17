@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 server.py --host 127.0.0.1 --port 8765
echo.
echo 网站服务已停止。按任意键关闭此窗口。
pause >nul
