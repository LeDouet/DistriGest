# -*- mode: python ; coding: utf-8 -*-
# ─────────────────────────────────────────────────────────────────
#  DISTRIGEST — PyInstaller Spec
#  STiNAUG TECHNOLOGIE — Abidjan, CI
#  Usage : pyinstaller DISTRIGEST.spec
# ─────────────────────────────────────────────────────────────────

import os, sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# ── Dossier racine du projet ──────────────────────────────────────
ROOT = os.path.abspath('.')

# ── Données à inclure (templates, static, etc.) ──────────────────
datas = [
    (os.path.join(ROOT, 'templates'),      'templates'),
    (os.path.join(ROOT, 'static'),         'static'),
    (os.path.join(ROOT, 'SumatraPDF.exe'), '.'),
]

# Ajouter les données Flask, Jinja2, Werkzeug, ReportLab
datas += collect_data_files('flask')
datas += collect_data_files('jinja2')
datas += collect_data_files('werkzeug')

try:
    datas += collect_data_files('reportlab')
except Exception:
    pass

# ── Imports cachés nécessaires ────────────────────────────────────
hidden_imports = [
    # Flask & dépendances
    'flask',
    'flask.templating',
    'flask.json',
    'jinja2',
    'jinja2.ext',
    'werkzeug',
    'werkzeug.routing',
    'werkzeug.security',
    'werkzeug.serving',
    'werkzeug.middleware.shared_data',
    'click',
    'itsdangerous',
    'markupsafe',

    # Serveur WSGI de production
    'waitress',
    'waitress.server',
    'waitress.task',
    'waitress.channel',
    'waitress.runner',

    # Base de données
    'sqlite3',

    # ReportLab PDF
    'reportlab',
    'reportlab.pdfgen',
    'reportlab.pdfgen.canvas',
    'reportlab.lib',
    'reportlab.lib.pagesizes',
    'reportlab.lib.colors',
    'reportlab.lib.styles',
    'reportlab.lib.units',
    'reportlab.lib.enums',
    'reportlab.platypus',
    'reportlab.platypus.tables',
    'reportlab.platypus.flowables',
    'reportlab.platypus.paragraph',

    # Standard library
    'email',
    'email.mime',
    'email.mime.text',
    'email.mime.multipart',
    'json',
    'logging',
    'threading',
    'webbrowser',
    'hashlib',
    'secrets',
    'datetime',
    're',
    'io',
    'os',
    'sys',
]

hidden_imports += collect_submodules('flask')
hidden_imports += collect_submodules('werkzeug')
hidden_imports += collect_submodules('jinja2')

# ── Analyse ───────────────────────────────────────────────────────
a = Analysis(
    ['lancer.py'],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'wx', 'gi', 'gtk',
        'matplotlib', 'numpy', 'pandas', 'scipy',
        'PIL', 'cv2', 'tensorflow', 'torch',
        'IPython', 'jupyter', 'notebook',
        'pytest', 'sphinx', 'docutils',
        '_tkinter',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ── Exécutable ────────────────────────────────────────────────────
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DISTRIGEST',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,                          # Pas de fenêtre console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='static\\distrigest.ico',        # Icône de l'exécutable
    version='version_info.txt',             # Infos version Windows
)

# ── Collecte (dossier dist) ───────────────────────────────────────
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DISTRIGEST',
)
