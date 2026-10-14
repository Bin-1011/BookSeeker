@echo off
rem BookSeeker 双击启动器（不弹黑框）
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

rem 优先用 pythonw（无控制台窗口），没有就退回 python
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "BookSeeker.py" %*
    goto :eof
)

where python >nul 2>nul
if %errorlevel%==0 (
    python "BookSeeker.py" %*
    goto :eof
)

echo.
echo   没有找到 Python。请先安装 Python 3.10 或更高版本：
echo   https://www.python.org/downloads/
echo.
echo   安装时记得勾选 "Add python.exe to PATH"。
echo.
pause
