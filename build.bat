@echo off
title DISTRIGEST - Build PyInstaller

echo.
echo ====================================================
echo   DISTRIGEST - Compilation PyInstaller
echo   STiNAUG TECHNOLOGIE - Abidjan, CI
echo ====================================================
echo.

:: Verifier Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python introuvable.
    echo Installez Python 3.10+ depuis https://python.org
    pause
    exit /b 1
)

:: Installer les dependances
:: NOTE : les ">=" DOIVENT etre entre guillemets, sinon batch les interprete
:: comme des redirections de sortie (creation de fichiers "=3.1.3" etc.)
echo [1/5] Installation des dependances principales...
pip install --quiet ^
    "flask>=3.1.3" ^
    "waitress>=3.0.2" ^
    "reportlab>=4.5.1" ^
    "python-escpos>=3.1" ^
    "pywin32>=312" ^
    "pyinstaller>=6.20.0"
if errorlevel 1 (
    echo [ERREUR] Echec installation des dependances principales.
    pause
    exit /b 1
)
echo       OK

:: Moteurs PDF optionnels — l'application utilise Microsoft Edge en
:: rendu headless si absents (preinstalle sur Windows 10/11).
:: Leur echec d'installation ne bloque PAS le build.
echo [1b/5] Moteurs PDF optionnels (weasyprint, pdfkit)...
pip install --quiet "weasyprint>=69.0" "pdfkit>=1.0.0" >nul 2>&1
if errorlevel 1 (
    echo       [INFO] weasyprint/pdfkit non installes — Edge sera utilise pour les PDF.
) else (
    echo       OK
)

:: Nettoyer les anciens builds
echo [2/5] Nettoyage anciens builds...
if exist "dist\DISTRIGEST" rmdir /s /q "dist\DISTRIGEST"
if exist "build\DISTRIGEST" rmdir /s /q "build\DISTRIGEST"
if exist "__pycache__" rmdir /s /q "__pycache__"
:: Supprimer les fichiers fantomes "=x.y.z" crees par l'ancien bug de redirection
del /q "=*" >nul 2>&1
echo       OK

:: Verifier les fichiers requis
echo [3/5] Verification des fichiers...
if not exist "lancer.py" (
    echo [ERREUR] lancer.py introuvable.
    pause
    exit /b 1
)
if not exist "distrigest.py" (
    echo [ERREUR] distrigest.py introuvable.
    pause
    exit /b 1
)
if not exist "templates" (
    echo [ERREUR] Dossier templates\ introuvable.
    pause
    exit /b 1
)
echo       OK

:: Lancer PyInstaller
echo [4/5] Compilation en cours (2-5 minutes)...
echo.
pyinstaller DISTRIGEST.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo [ERREUR] Compilation echouee.
    pause
    exit /b 1
)

:: Finalisation
echo.
echo [5/5] Finalisation...

:: Copier templates
xcopy /e /i /q /y "templates" "dist\DISTRIGEST\templates" >nul

:: Copier static
if exist "static" (
    xcopy /e /i /q /y "static" "dist\DISTRIGEST\static" >nul
)

:: Dossier donnees
if not exist "dist\DISTRIGEST\data" mkdir "dist\DISTRIGEST\data"

:: Verifier l'exe
if not exist "dist\DISTRIGEST\DISTRIGEST.exe" (
    echo [ERREUR] Executable non cree.
    pause
    exit /b 1
)

echo.
echo ====================================================
echo   BUILD REUSSI !
echo   dist\DISTRIGEST\DISTRIGEST.exe
echo ====================================================
echo.
echo Testez : dist\DISTRIGEST\DISTRIGEST.exe
echo Puis   : build_setup.bat pour l'installeur
echo.
pause
