#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lancer.py — Lanceur DISTRIGEST
STiNAUG TECHNOLOGIE · Abidjan, Côte d'Ivoire
"""

import os, sys, socket, time, threading, webbrowser, traceback

PORT = 1439
URL  = f"http://localhost:{PORT}"

# ── Répertoire de travail ─────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

ERREURS = []

# ─────────────────────────────────────────────────────────────────────
def banner():
    print()
    print("  ╔══════════════════════════════════════════════════╗")
    print("  ║   🛒  DISTRIGEST                              ║")
    print("  ║   v2.0  ·  Gestion commerciale            ║")
    print("  ║   STiNAUG TECHNOLOGIE                            ║")
    print("  ╚══════════════════════════════════════════════════╝")
    print()

def ok(msg):   print(f"  ✅ {msg}")
def err(msg):  print(f"  ❌ {msg}"); ERREURS.append(msg)
def warn(msg): print(f"  ⚠  {msg}")
def info(msg): print(f"  ➜  {msg}")

def pause():
    print()
    try:
        input("  Appuyez sur Entrée pour quitter…")
    except (KeyboardInterrupt, EOFError):
        pass
    sys.exit(1)

# ── Vérification port ─────────────────────────────────────────────────
def port_libre(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', port)) != 0

# ── Ouvrir navigateur ─────────────────────────────────────────────────
def ouvrir_navigateur(delai=2.5):
    time.sleep(delai)
    webbrowser.open(URL)

# ─────────────────────────────────────────────────────────────────────
def verifications():
    print("  Démarrage en cours…")
    print()

    # Python
    v = sys.version_info
    ok(f"Python {v.major}.{v.minor}.{v.micro}")
    if v.major < 3 or (v.major == 3 and v.minor < 9):
        err("Python 3.9+ requis")

    # Fichier principal
    app_file = os.path.join(BASE_DIR, 'distrigest.py')
    if not getattr(sys, 'frozen', False) and not os.path.exists(app_file):
        err(f"Fichier manquant : distrigest.py")
    else:
        ok("Application : distrigest.py")

    # Dossier templates
    tpl = os.path.join(BASE_DIR, 'templates')
    if not os.path.isdir(tpl):
        err(f"Dossier manquant : templates/")
    else:
        ok(f"Templates : {len(os.listdir(tpl))} fichiers")

    # Dossier static
    sta = os.path.join(BASE_DIR, 'static')
    if not os.path.isdir(sta):
        warn("Dossier static/ absent (non bloquant)")
    else:
        ok("Static trouvé")

    # Flask
    try:
        import flask
        try:
            import importlib.metadata as _m
            _fv = _m.version('flask')
        except Exception:
            _fv = getattr(flask, '__version__', 'OK')
        ok(f"Flask {_fv}")
    except ImportError:
        err("Flask non installé  →  pip install flask")

    # Waitress
    try:
        import waitress
        try:
            import importlib.metadata as _m
            _wv = _m.version('waitress')
        except Exception:
            _wv = 'OK'
        ok(f"Waitress {_wv}")
    except ImportError:
        warn("Waitress absent — utilisation Flask dev server")

    # SQLite (stdlib)
    try:
        import sqlite3
        ok(f"SQLite {sqlite3.sqlite_version}")
    except ImportError:
        err("sqlite3 introuvable (anormal)")

    # Base de données existante
    # L'application stocke la base dans data\distrigest.db (cf. distrigest.py).
    # On vérifie ce chemin en priorité, puis la racine (anciennes installations).
    import glob as _glob
    _data_dir = os.path.join(BASE_DIR, 'data')
    existing  = _glob.glob(os.path.join(_data_dir, '*.db'))
    # Fallback racine (anciennes installations)
    if not existing and os.path.exists(os.path.join(BASE_DIR, 'distrigest.db')):
        existing = [os.path.join(BASE_DIR, 'distrigest.db')]
    if existing:
        db_path = max(existing, key=os.path.getmtime)   # la plus récente = la bonne
        size_kb = os.path.getsize(db_path) // 1024
        rel     = os.path.relpath(db_path, BASE_DIR)
        ok(f"Base de données : {rel} ({size_kb} Ko)")
    else:
        info("Nouvelle base de données — sera créée au démarrage")

    # Droits d'écriture
    try:
        test_file = os.path.join(BASE_DIR, '.write_test')
        with open(test_file, 'w') as f:
            f.write('ok')
        os.remove(test_file)
        ok("Droits d'écriture : OK")
    except Exception:
        err(f"Pas de droits d'écriture dans : {BASE_DIR}")

    # Port réseau
    if not port_libre(PORT):
        warn(f"Port {PORT} déjà utilisé — serveur déjà en cours")
        info(f"Ouverture du navigateur sur {URL} …")
        print()
        webbrowser.open(URL)
        sys.exit(0)
    else:
        ok(f"Port {PORT} disponible")

    print()

    # Bilan
    if ERREURS:
        print("  ┌─────────────────────────────────────────────────┐")
        print("  │  ❌ ERREURS DÉTECTÉES — impossible de démarrer  │")
        print("  └─────────────────────────────────────────────────┘")
        for e in ERREURS:
            print(f"     • {e}")
        pause()

# ─────────────────────────────────────────────────────────────────────
def main():
    banner()
    verifications()

    ok(f"Démarrage sur {URL}")
    info(f"Répertoire : {BASE_DIR}")
    print(f"  ⛔ Ctrl+C pour arrêter")
    print()

    threading.Thread(target=ouvrir_navigateur, daemon=True).start()

    try:
        try:
            from waitress import serve
            from distrigest import app, init_db
            init_db()
            print(f"  🚀 Serveur Waitress actif — {URL}\n")
            serve(app, host='0.0.0.0', port=PORT, threads=4)
        except ImportError:
            from distrigest import app, init_db
            init_db()
            print(f"  🚀 Serveur Flask actif — {URL}\n")
            app.run(host='0.0.0.0', port=PORT, debug=False)

    except KeyboardInterrupt:
        print("\n  🛑 Arrêt de DISTRIGEST.")
        sys.exit(0)

    except OSError as e:
        print(f"\n  ❌ Erreur réseau : {e}")
        if 'Address already in use' in str(e) or '10048' in str(e):
            warn(f"Port {PORT} occupé par un autre processus")
            info("Fermez l'autre instance ou redémarrez le PC")
        pause()

    except ModuleNotFoundError as e:
        print(f"\n  ❌ Module manquant : {e}")
        info("Lancez :  pip install -r requirements.txt")
        pause()

    except Exception as e:
        print(f"\n  ❌ Erreur inattendue : {e}")
        print()
        traceback.print_exc()
        pause()

if __name__ == '__main__':
    main()
