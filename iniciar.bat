@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   DESCARGADOR DEHU QEV
echo ============================================================
echo.

REM --- Detectar interprete Python -----------------------------
set "PYTHON_CMD="
where py >nul 2>&1
if !errorlevel! equ 0 (
    py -3.11 --version >nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON_CMD=py -3.11"
    ) else (
        py -3 --version >nul 2>&1
        if !errorlevel! equ 0 set "PYTHON_CMD=py -3"
    )
)
if "!PYTHON_CMD!"=="" (
    where python >nul 2>&1
    if !errorlevel! equ 0 set "PYTHON_CMD=python"
)
if "!PYTHON_CMD!"=="" (
    echo [ERROR] No se encuentra Python instalado.
    echo         Instala Python 3.11+ desde https://www.python.org/downloads/
    echo         y vuelve a ejecutar este script.
    pause
    exit /b 1
)
echo [OK] Python detectado: !PYTHON_CMD!

REM --- Crear venv si no existe --------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [INFO] Creando entorno virtual en .venv ...
    !PYTHON_CMD! -m venv .venv
    if !errorlevel! neq 0 (
        echo [ERROR] No se pudo crear el venv.
        pause
        exit /b 1
    )
    echo [OK] venv creado.

    echo.
    echo [INFO] Actualizando pip ...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip

    echo.
    echo [INFO] Instalando dependencias de requirements.txt ...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if !errorlevel! neq 0 (
        echo [ERROR] Fallo instalando dependencias.
        pause
        exit /b 1
    )
    echo [OK] Dependencias instaladas.
) else (
    echo [OK] venv ya existente.
)

REM --- Lanzar app ---------------------------------------------
echo.
echo [INFO] Iniciando servidor Flask en http://localhost:60004 ...
echo        Cierra esta ventana o pulsa Ctrl+C para detener.
echo.
".venv\Scripts\python.exe" app.py

REM Si el servidor termina por error, pausar para ver mensaje
if !errorlevel! neq 0 (
    echo.
    echo [WARN] El servidor termino con codigo !errorlevel!.
    pause
)

endlocal
