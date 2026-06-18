@echo off
title DISTRIGEST - Build Installeur

echo.
echo ====================================================
echo   DISTRIGEST - Creation de l'installeur .exe
echo   STiNAUG TECHNOLOGIE - Abidjan, CI
echo ====================================================
echo.

:: Verifier que le build PyInstaller existe
if not exist "dist\DISTRIGEST\DISTRIGEST.exe" (
    echo [ERREUR] Executable PyInstaller introuvable.
    echo Lancez d'abord build.bat
    pause
    exit /b 1
)

:: Creer le dossier installeur
if not exist "installer" mkdir "installer"

:: Chercher Inno Setup
set ISCC=
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe
if exist "C:\Program Files\Inno Setup 6\ISCC.exe"       set ISCC=C:\Program Files\Inno Setup 6\ISCC.exe
if exist "C:\Program Files (x86)\Inno Setup 5\ISCC.exe" set ISCC=C:\Program Files (x86)\Inno Setup 5\ISCC.exe

if "%ISCC%"=="" (
    echo [ERREUR] Inno Setup introuvable.
    echo Telechargez : https://jrsoftware.org/isinfo.php
    pause
    exit /b 1
)

echo [INFO] Inno Setup : %ISCC%
echo.

:: Compiler l'installeur
echo [1/2] Compilation de l'installeur...
"%ISCC%" "DISTRIGEST_setup.iss"
if errorlevel 1 (
    echo [ERREUR] Compilation installeur echouee.
    pause
    exit /b 1
)

:: Verifier le resultat
echo.
echo [2/2] Verification...
for %%f in (installer\*.exe) do (
    echo.
    echo ====================================================
    echo   INSTALLEUR CREE : %%f
    echo ====================================================
    echo.
    explorer "installer"
    goto end
)

echo [ERREUR] Aucun installeur trouve dans installer\

:end
pause
