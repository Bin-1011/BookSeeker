@echo off
rem ============================================================
rem   BookSeeker 免安装 exe 一键构建
rem   产物：dist\BookSeeker.exe（图形界面） / dist\BookSeeker-cli.exe（命令行）
rem ============================================================
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   没有找到 Python。请先安装 Python 3.10 或更高版本：
    echo   https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

if not exist ".build\tmp" mkdir ".build\tmp" 2>nul

rem 构建用的临时目录一律放在项目内，避免受系统 %TEMP% 权限影响
set "TEMP=%CD%\.build\tmp"
set "TMP=%CD%\.build\tmp"
set "PYTHONPATH=%CD%\.build\pylibs"
set "PYINSTALLER_CONFIG_DIR=%CD%\.build\pyi-cache"

if exist ".build\pylibs\PyInstaller" goto :have_pyi
echo [1/3] 安装 PyInstaller 到 .build\pylibs ...
python -m pip install --no-cache-dir --no-warn-script-location --target ".build\pylibs" pyinstaller
if errorlevel 1 goto :fail
goto :pyi_ok

:have_pyi
echo [1/3] PyInstaller 已就绪，跳过安装

:pyi_ok
echo [2/3] 检查图标 ...
if not exist "docs\icon.ico" echo       注意：没有 docs\icon.ico，将使用默认图标

echo [3/3] 开始打包（第一次大约需要 1-2 分钟）...
python -m PyInstaller BookSeeker.spec --noconfirm --workpath ".build\pyi-work" --distpath "dist"
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo   构建完成，产物在 dist\ 目录：
echo.
echo     dist\BookSeeker.exe       双击启动图形界面
echo     dist\BookSeeker-cli.exe   在命令行里使用
echo.
echo   整个 exe 是免安装的：拷到任何 Windows 电脑双击即可运行，
echo   不需要装 Python。程序数据会存在 exe 旁边的 BookSeeker-data\ 里。
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo   构建失败，请检查上面的错误信息。
echo.
pause
exit /b 1
