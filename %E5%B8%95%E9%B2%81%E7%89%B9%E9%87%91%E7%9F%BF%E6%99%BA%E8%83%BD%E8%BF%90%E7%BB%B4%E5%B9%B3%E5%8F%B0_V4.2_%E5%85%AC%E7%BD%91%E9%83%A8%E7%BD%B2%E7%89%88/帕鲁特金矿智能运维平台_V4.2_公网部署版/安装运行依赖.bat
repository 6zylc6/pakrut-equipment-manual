@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 -m pip install -r requirements.txt
echo.
echo 依赖安装完成。按任意键关闭此窗口。
pause >nul
