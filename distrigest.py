#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DISTRIGEST — Gestion Commerciale
STiNAUG TECHNOLOGIE · Abidjan, Côte d'Ivoire
Flask + SQLite · Port 1439
"""

import os, sys, sqlite3, json, re, io, hashlib, random, string
import smtplib, ssl, threading, time as _time_module
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, date, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, g)

# ── Notifications SMS / WhatsApp : envoi via API REST (urllib, stdlib) ──
# SMS : passerelle locale ivoirienne · WhatsApp : API Cloud officielle Meta.
# Aucune dépendance externe (Twilio retiré).

# ── Résolution des chemins PyInstaller ──────────────────────────────
# Quand lancé via PyInstaller (frozen), les fichiers sont dans _MEIPASS
# Quand lancé en développement, ils sont à côté du script
if getattr(sys, 'frozen', False):
    # Mode PyInstaller — dossier de l'exe
    _BASE_DIR = os.path.dirname(sys.executable)
    _BUNDLE   = sys._MEIPASS  # fichiers embarqués (templates, static)
else:
    # Mode développement
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _BUNDLE   = _BASE_DIR

# Changer le répertoire de travail → dossier de l'exe
os.chdir(_BASE_DIR)

# ── Logging vers terminal (console visible) ───────────────────────────
import logging
_LOG_DIR  = os.path.join(os.environ.get('APPDATA', _BASE_DIR), 'DISTRIGEST')
_LOG_PATH = os.path.join(_LOG_DIR, 'distrigest.log')
os.makedirs(_LOG_DIR, exist_ok=True)   # créer le dossier AVANT d'ouvrir le fichier

# StreamHandler UTF-8 pour Windows CMD (évite UnicodeEncodeError avec emojis)
_stream_handler = logging.StreamHandler(stream=sys.stdout)
try:
    _stream_handler.stream.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass  # Python < 3.7 ou terminal non reconfigurable

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        _stream_handler,
        logging.FileHandler(_LOG_PATH, encoding='utf-8'),
    ]
)
logging.info("Démarrage DISTRIGEST depuis %s", _BASE_DIR)


# ── Application Flask avec chemins corrigés ──────────────────────────
app = Flask(
    __name__,
    template_folder=os.path.join(_BUNDLE, 'templates'),
    static_folder=os.path.join(_BUNDLE, 'static'),
)
app.secret_key                      = 'STINAUGTECH2025-DISTRIGEST-S3CR3T-K3Y-!@#'
app.config['MAX_CONTENT_LENGTH']    = 500 * 1024 * 1024  # 500 Mo max par requête (images base64)
app.config['PROPAGATE_EXCEPTIONS'] = True
# ── Répertoires de données (écriture persistante) ────────────────────
# data/<slug>.db → nommé d'après le nom configuré de l'entreprise
_DATA_DIR   = os.path.join(_BASE_DIR, 'data')
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
except OSError as _e:
    print(f"[ERREUR] Impossible de créer le dossier data/ : {_e}")
    print(f"[INFO]   BASE_DIR = {_BASE_DIR}")
    print(f"[INFO]   DATA_DIR = {_DATA_DIR}")

def _slugify_db(nom):
    """Convertit un nom d'entreprise en nom de fichier sûr (ASCII, sans espaces)."""
    import re as _re, unicodedata as _ud
    s = _ud.normalize('NFD', nom or '')
    s = s.encode('ascii', 'ignore').decode('ascii')
    s = _re.sub(r'[^\w\s-]', '', s).strip()
    s = _re.sub(r'[\s_-]+', '_', s)
    return (s[:40] or 'distrigest').lower()

def _resolve_db_path():
    """Détermine le chemin de la base de données :
    1. Migration transparente : si une seule *.db existe dans data/, on l'utilise.
    2. Par défaut : distrigest.db (sera renommée dès le 1er enregistrement des paramètres)."""
    import glob as _glob
    existing = _glob.glob(os.path.join(_DATA_DIR, '*.db'))
    if len(existing) == 1:
        return existing[0]   # base unique — on la prend telle quelle
    if len(existing) > 1:
        # Plusieurs bases : prendre la plus récente
        return max(existing, key=os.path.getmtime)
    return os.path.join(_DATA_DIR, 'distrigest.db')

DB_PATH = _resolve_db_path()
print(f"[INFO] Base de données : {DB_PATH}")

# ══════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════
def get_db():
    if 'db' not in g:
        # Sécurité : s'assurer que le dossier data/ existe avant chaque connexion
        # (utile si makedirs a échoué silencieusement au chargement du module)
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
        except OSError as _e:
            logging.error("Impossible de créer le dossier data/ : %s", _e)
            raise
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


def _run_startup_migrations():
    """Migrations idempotentes exécutées au chargement de l'app (Waitress ou flask run).
    Ajoute les colonnes manquantes sans casser les bases existantes.
    Note : SQLite refuse ALTER TABLE ADD COLUMN ... UNIQUE, donc on ajoute la colonne
    sans contrainte puis on crée un index UNIQUE séparément."""
    if not os.path.exists(DB_PATH):
        return  # la création initiale s'en occupe
    try:
        _db = sqlite3.connect(DB_PATH)
        # ── Migration : username sur utilisateurs ──
        try:
            _db.execute("ALTER TABLE utilisateurs ADD COLUMN username TEXT")
            _db.commit()
            logging.info("[MIGRATION] colonne 'username' ajoutée à utilisateurs")
        except sqlite3.OperationalError:
            pass  # déjà présente
        try:
            _db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_utilisateurs_username ON utilisateurs(username)")
            _db.commit()
        except sqlite3.OperationalError as _e:
            logging.warning("[MIGRATION] index username : %s", _e)
        # ── Migration : nettoyage des doublons dans ecritures_comptables ──
        # Supprime les écritures en doublon (même source + source_id),
        # en gardant uniquement la première (id le plus bas).
        # Idempotent : ne fait rien si la base est déjà propre.
        try:
            nb_doublons = _db.execute("""
                SELECT COUNT(*) FROM ecritures_comptables
                WHERE source IS NOT NULL
                  AND id NOT IN (
                      SELECT MIN(id) FROM ecritures_comptables
                      WHERE source IS NOT NULL
                      GROUP BY source, source_id
                  )
            """).fetchone()[0]
            if nb_doublons > 0:
                _db.execute("""
                    DELETE FROM ecritures_comptables
                    WHERE source IS NOT NULL
                      AND id NOT IN (
                          SELECT MIN(id) FROM ecritures_comptables
                          WHERE source IS NOT NULL
                          GROUP BY source, source_id
                      )
                """)
                _db.commit()
                logging.info("[MIGRATION] %d doublon(s) supprimé(s) dans ecritures_comptables", nb_doublons)
        except sqlite3.OperationalError as _e:
            logging.warning("[MIGRATION] nettoyage doublons ecritures : %s", _e)
        # ── Migration : index UNIQUE (source, source_id) sur ecritures_comptables ──
        # Bloque définitivement tout futur doublon au niveau de la base.
        # WHERE source IS NOT NULL exclut les écritures manuelles (source=NULL).
        try:
            _db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_ecritures_source_unique
                           ON ecritures_comptables(source, source_id)
                           WHERE source IS NOT NULL""")
            _db.commit()
            logging.info("[MIGRATION] index UNIQUE (source, source_id) créé sur ecritures_comptables")
        except sqlite3.OperationalError as _e:
            logging.warning("[MIGRATION] index ecritures source_unique : %s", _e)
        # ── Migration : purge des écritures 'reglement' en double comptage ──
        # Un règlement lié à une facture/vente est déjà comptabilisé via
        # l'écriture 'facture' (montant_paye) ; un règlement fournisseur via
        # l'écriture 'reglement_fourn'. Les écritures 'reglement' correspondantes
        # font donc double emploi → on les supprime. Idempotent.
        try:
            nb_dbl = _db.execute("""
                SELECT COUNT(*) FROM ecritures_comptables
                WHERE source='reglement'
                  AND source_id IN (
                      SELECT id FROM reglements
                      WHERE source_type IN ('facture','vente','facture_fourn')
                  )
            """).fetchone()[0]
            if nb_dbl > 0:
                _db.execute("""
                    DELETE FROM ecritures_comptables
                    WHERE source='reglement'
                      AND source_id IN (
                          SELECT id FROM reglements
                          WHERE source_type IN ('facture','vente','facture_fourn')
                      )
                """)
                _db.commit()
                logging.info("[MIGRATION] %d écriture(s) 'reglement' en double comptage supprimée(s)", nb_dbl)
        except sqlite3.OperationalError as _e:
            logging.warning("[MIGRATION] purge double comptage reglement : %s", _e)
        # ── Migration : purge des écritures 'achat' (TTC complet) en double comptage ──
        # L'ancienne logique enregistrait le TTC complet d'une commande reçue
        # comme dépense (source='achat'). En comptabilité de caisse, seule la
        # sortie réelle compte : elle provient désormais des règlements
        # (source_type='achat' → écriture 'reglement_achat'). Les écritures
        # 'achat' historiques font donc double emploi → on les supprime. Idempotent.
        try:
            nb_ach = _db.execute(
                "SELECT COUNT(*) FROM ecritures_comptables WHERE source='achat'"
            ).fetchone()[0]
            if nb_ach > 0:
                _db.execute("DELETE FROM ecritures_comptables WHERE source='achat'")
                _db.commit()
                logging.info("[MIGRATION] %d écriture(s) 'achat' (TTC complet) en double comptage supprimée(s)", nb_ach)
        except sqlite3.OperationalError as _e:
            logging.warning("[MIGRATION] purge double comptage achat : %s", _e)
        # ── Migration : purge des fausses recettes 'reglement' sur paiements fournisseurs ──
        # Un paiement fournisseur (reglement.fournisseur_id renseigné, ex. source_type
        # 'achat') a pu être importé à tort comme RECETTE client (source='reglement').
        # C'est une sortie, jamais une recette → on supprime ces écritures. Idempotent.
        try:
            nb_fx = _db.execute("""
                SELECT COUNT(*) FROM ecritures_comptables
                WHERE source='reglement' AND type_ecriture='recette'
                  AND source_id IN (SELECT id FROM reglements WHERE fournisseur_id IS NOT NULL)
            """).fetchone()[0]
            if nb_fx > 0:
                _db.execute("""
                    DELETE FROM ecritures_comptables
                    WHERE source='reglement' AND type_ecriture='recette'
                      AND source_id IN (SELECT id FROM reglements WHERE fournisseur_id IS NOT NULL)
                """)
                _db.commit()
                logging.info("[MIGRATION] %d fausse(s) recette 'reglement' (paiement fournisseur) supprimée(s)", nb_fx)
        except sqlite3.OperationalError as _e:
            logging.warning("[MIGRATION] purge fausses recettes fournisseur : %s", _e)
        # ── Migration : prenom sur utilisateurs ──
        try:
            _db.execute("ALTER TABLE utilisateurs ADD COLUMN prenom TEXT")
            _db.commit()
            logging.info("[MIGRATION] colonne 'prenom' ajoutée à utilisateurs")
        except sqlite3.OperationalError:
            pass  # déjà présente
        # ── Migration : module Atelier (équipements + tickets) ──
        for _sql in [
            """CREATE TABLE IF NOT EXISTS equipements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL REFERENCES clients(id),
                type_appareil TEXT NOT NULL DEFAULT 'Autre',
                marque TEXT, modele TEXT, numero_serie TEXT,
                couleur TEXT, description TEXT,
                date_creation TEXT DEFAULT (date('now')))""",
            """CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference TEXT UNIQUE,
                client_id INTEGER NOT NULL REFERENCES clients(id),
                equipement_id INTEGER REFERENCES equipements(id),
                type_panne TEXT, description_panne TEXT,
                accessoires TEXT, mot_de_passe TEXT,
                statut TEXT DEFAULT 'recu', priorite TEXT DEFAULT 'normale',
                technicien_id INTEGER REFERENCES employes(id),
                date_reception TEXT DEFAULT (date('now')),
                date_prevue TEXT, date_cloture TEXT,
                diagnostic TEXT, travaux_effectues TEXT,
                cout_estime REAL DEFAULT 0, cout_final REAL DEFAULT 0,
                montant_regle REAL DEFAULT 0, notes TEXT,
                date_creation TEXT DEFAULT (datetime('now')))""",
            """CREATE TABLE IF NOT EXISTS reglements_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                client_id INTEGER REFERENCES clients(id),
                montant REAL NOT NULL,
                mode_paiement TEXT DEFAULT 'especes',
                date_reglement TEXT DEFAULT (date('now')),
                notes TEXT,
                date_creation TEXT DEFAULT (datetime('now')))""",
        ]:
            try:
                _db.execute(_sql)
                _db.commit()
            except sqlite3.OperationalError:
                pass
        for _param in [('module_atelier','non'),
                       ('acces_atelier_equipements','ecriture'),
                       ('acces_atelier_tickets','ecriture')]:
            try:
                _db.execute("INSERT OR IGNORE INTO parametres(cle,valeur) VALUES(?,?)", _param)
                _db.commit()
            except Exception:
                pass
        # ── Migration : table historique_notifications ──
        try:
            _db.execute("""
                CREATE TABLE IF NOT EXISTS historique_notifications (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id   INTEGER REFERENCES clients(id),
                    facture_id  INTEGER REFERENCES documents_vente(id),
                    type_notif  TEXT DEFAULT 'relance',
                    canal       TEXT NOT NULL,
                    statut      TEXT DEFAULT 'ok',
                    montant_du  REAL DEFAULT 0,
                    erreur      TEXT,
                    date_envoi  TEXT DEFAULT (datetime('now'))
                )
            """)
            _db.commit()
            logging.info("[MIGRATION] table historique_notifications OK")
        except sqlite3.OperationalError:
            pass
        # ── Migration : colonnes facture_id + type_notif sur historique_notifications ──
        for _col, _def in [('facture_id', 'INTEGER'), ('type_notif', "TEXT DEFAULT 'relance'")]:
            try:
                _db.execute(f"ALTER TABLE historique_notifications ADD COLUMN {_col} {_def}")
                _db.commit()
                logging.info("[MIGRATION] colonne '%s' ajoutée à historique_notifications", _col)
            except sqlite3.OperationalError:
                pass
        _db.close()
    except Exception as _e:
        logging.error("[MIGRATION] erreur : %s", _e)


# Exécution immédiate au chargement du module (couvre Waitress qui ne passe pas par __main__)
_run_startup_migrations()

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rv = cur.fetchall()
    return (rv[0] if rv else None) if one else rv

def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid

def get_default_depot_id():
    """Retourne l'id du premier dépôt actif, ou None si aucun dépôt n'existe."""
    row = query("SELECT id FROM depots WHERE actif=1 ORDER BY id LIMIT 1", one=True)
    return row['id'] if row else None

def resolve_depot_id(value):
    """Résout un depot_id depuis un champ formulaire.
    Retourne l'id si valide, sinon le premier dépôt actif, sinon None."""
    try:
        did = int(value) if value else None
    except (TypeError, ValueError):
        did = None
    if did:
        exists = query("SELECT id FROM depots WHERE id=?", (did,), one=True)
        if exists:
            return did
    return get_default_depot_id()

def _safe_fk(value):
    """Nettoie une valeur de clé étrangère venue d'un formulaire.
    Retourne un int valide, ou None pour '', 'None', '0', 'null', ou toute
    valeur non numérique. Évite l'erreur 'FOREIGN KEY constraint failed'
    causée par Jinja rendant None comme la chaîne littérale 'None'."""
    if value is None:
        return None
    s = str(value).strip()
    if s in ('', 'None', '0', 'null', 'NULL'):
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    c = db.cursor()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS parametres (
        id INTEGER PRIMARY KEY,
        cle TEXT UNIQUE NOT NULL,
        valeur TEXT
    );

    CREATE TABLE IF NOT EXISTS utilisateurs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT NOT NULL,
        prenom TEXT,
        username TEXT UNIQUE,
        email TEXT UNIQUE,
        mot_de_passe TEXT NOT NULL,
        role TEXT DEFAULT 'gestionnaire',
        actif INTEGER DEFAULT 1,
        date_creation TEXT DEFAULT (date('now'))
    );

    CREATE TABLE IF NOT EXISTS familles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        nom TEXT NOT NULL,
        couleur TEXT DEFAULT '#2563eb',
        ordre INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS unites_vente (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT NOT NULL UNIQUE,
        actif INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reference TEXT UNIQUE,
        designation TEXT NOT NULL,
        famille_id INTEGER REFERENCES familles(id),
        contenance TEXT,
        colisage INTEGER DEFAULT 1,
        unite_vente TEXT DEFAULT 'Bouteille',
        prix_achat_ht REAL DEFAULT 0,
        prix_vente_ht REAL DEFAULT 0,
        tva REAL DEFAULT 0,
        code_barre TEXT,
        fournisseur_id INTEGER,
        date_peremption TEXT,
        actif INTEGER DEFAULT 1,
        notes TEXT,
        icone TEXT DEFAULT '📦',
        date_creation TEXT DEFAULT (date('now'))
    );

    CREATE TABLE IF NOT EXISTS depots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        nom TEXT NOT NULL,
        adresse TEXT,
        responsable TEXT,
        actif INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS stocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        article_id INTEGER NOT NULL REFERENCES articles(id),
        depot_id INTEGER NOT NULL REFERENCES depots(id),
        quantite_unite REAL DEFAULT 0,
        quantite_colis REAL DEFAULT 0,
        stock_min_unite INTEGER DEFAULT 0,
        UNIQUE(article_id, depot_id)
    );

    CREATE TABLE IF NOT EXISTS mouvements_stocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        article_id INTEGER REFERENCES articles(id),
        depot_id INTEGER REFERENCES depots(id),
        type_mvt TEXT NOT NULL,
        quantite_unite REAL DEFAULT 0,
        quantite_colis REAL DEFAULT 0,
        prix_unitaire REAL DEFAULT 0,
        doc_type TEXT,
        doc_id INTEGER,
        doc_ref TEXT,
        notes TEXT,
        date_mvt TEXT DEFAULT (date('now')),
        operateur TEXT
    );

    CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        nom TEXT NOT NULL,
        prenom TEXT,
        type_client TEXT DEFAULT 'particulier',
        telephone TEXT,
        telephone2 TEXT,
        email TEXT,
        adresse TEXT,
        ville TEXT DEFAULT 'Abidjan',
        zone_livraison TEXT,
        encours_autorise REAL DEFAULT 0,
        encours_actuel REAL DEFAULT 0,
        remise_pct REAL DEFAULT 0,
        mode_paiement TEXT DEFAULT 'especes',
        delai_paiement INTEGER DEFAULT 0,
        representant_id INTEGER REFERENCES representants(id),
        actif INTEGER DEFAULT 1,
        notes TEXT,
        date_creation TEXT DEFAULT (date('now'))
    );

    CREATE TABLE IF NOT EXISTS fournisseurs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        nom TEXT NOT NULL,
        contact TEXT,
        telephone TEXT,
        email TEXT,
        adresse TEXT,
        ville TEXT,
        pays TEXT DEFAULT 'Côte d''Ivoire',
        type_produits TEXT,
        delai_livraison TEXT,
        conditions_paiement TEXT,
        remise_pct REAL DEFAULT 0,
        actif INTEGER DEFAULT 1,
        notes TEXT,
        date_creation TEXT DEFAULT (date('now'))
    );

    CREATE TABLE IF NOT EXISTS documents_vente (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type_doc TEXT NOT NULL,
        reference TEXT UNIQUE,
        client_id INTEGER REFERENCES clients(id),
        depot_id INTEGER REFERENCES depots(id),
        date_doc TEXT DEFAULT (date('now')),
        date_livraison TEXT,
        date_echeance TEXT,
        statut TEXT DEFAULT 'en_attente',
        remise_globale REAL DEFAULT 0,
        total_ht REAL DEFAULT 0,
        total_tva REAL DEFAULT 0,
        total_ttc REAL DEFAULT 0,
        montant_paye REAL DEFAULT 0,
        reste REAL DEFAULT 0,
        mode_paiement TEXT DEFAULT 'especes',
        livreur TEXT,
        notes TEXT,
        doc_parent_id INTEGER,
        date_creation TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS lignes_vente (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES documents_vente(id) ON DELETE CASCADE,
        article_id INTEGER REFERENCES articles(id),
        designation TEXT,
        quantite_unite REAL DEFAULT 0,
        quantite_colis REAL DEFAULT 0,
        prix_ht REAL DEFAULT 0,
        remise_pct REAL DEFAULT 0,
        tva REAL DEFAULT 0,
        total_ht REAL DEFAULT 0,
        total_ttc REAL DEFAULT 0,
        num_ligne INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS documents_achat (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type_doc TEXT NOT NULL,
        reference TEXT UNIQUE,
        fournisseur_id INTEGER REFERENCES fournisseurs(id),
        depot_id INTEGER REFERENCES depots(id),
        date_doc TEXT DEFAULT (date('now')),
        date_livraison_prevue TEXT,
        statut TEXT DEFAULT 'en_attente',
        total_ht REAL DEFAULT 0,
        total_tva REAL DEFAULT 0,
        total_ttc REAL DEFAULT 0,
        montant_paye REAL DEFAULT 0,
        reste REAL DEFAULT 0,
        notes TEXT,
        date_creation TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS lignes_achat (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES documents_achat(id) ON DELETE CASCADE,
        article_id INTEGER REFERENCES articles(id),
        designation TEXT,
        quantite_unite REAL DEFAULT 0,
        quantite_colis REAL DEFAULT 0,
        prix_achat_ht REAL DEFAULT 0,
        tva REAL DEFAULT 0,
        total_ht REAL DEFAULT 0,
        total_ttc REAL DEFAULT 0,
        qte_recue REAL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS reglements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reference TEXT UNIQUE,
        source_type TEXT NOT NULL,
        source_id INTEGER,
        client_id INTEGER,
        fournisseur_id INTEGER,
        montant REAL NOT NULL,
        mode_paiement TEXT DEFAULT 'especes',
        date_reglement TEXT DEFAULT (date('now')),
        notes TEXT,
        date_creation TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS depenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        categorie TEXT NOT NULL,
        description TEXT NOT NULL,
        montant REAL NOT NULL,
        date_depense TEXT DEFAULT (date('now')),
        responsable TEXT,
        notes TEXT
    );

    CREATE TABLE IF NOT EXISTS employes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        matricule TEXT UNIQUE,
        nom TEXT NOT NULL,
        prenom TEXT,
        poste TEXT,
        telephone TEXT,
        email TEXT,
        salaire_base REAL DEFAULT 0,
        date_embauche TEXT,
        statut TEXT DEFAULT 'actif',
        notes TEXT
    );

    CREATE TABLE IF NOT EXISTS fiches_paie (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employe_id INTEGER NOT NULL REFERENCES employes(id),
        mois TEXT NOT NULL,
        salaire_base REAL DEFAULT 0,
        prime_transport REAL DEFAULT 0,
        prime_anciennete REAL DEFAULT 0,
        autres_primes REAL DEFAULT 0,
        retenue_absence REAL DEFAULT 0,
        autres_retenues REAL DEFAULT 0,
        salaire_brut REAL DEFAULT 0,
        salaire_net REAL DEFAULT 0,
        statut TEXT DEFAULT 'brouillon',
        mode_paiement TEXT DEFAULT 'especes',
        date_paiement TEXT,
        notes TEXT,
        date_creation TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS conges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employe_id INTEGER NOT NULL REFERENCES employes(id),
        type_conge TEXT DEFAULT 'annuel',
        date_debut TEXT NOT NULL,
        date_fin TEXT NOT NULL,
        nb_jours INTEGER DEFAULT 1,
        statut TEXT DEFAULT 'en_attente',
        motif TEXT,
        notes TEXT,
        date_creation TEXT DEFAULT (datetime('now'))
    );


    CREATE TABLE IF NOT EXISTS types_emballages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        nom TEXT NOT NULL,
        categorie TEXT DEFAULT 'bouteille',
        contenance TEXT,
        prix_consigne REAL DEFAULT 0,
        prix_achat REAL DEFAULT 0,
        duree_vie_mois INTEGER DEFAULT 60,
        nb_rotations_max INTEGER DEFAULT 0,
        couleur TEXT DEFAULT '#3b82f6',
        actif INTEGER DEFAULT 1,
        notes TEXT,
        date_creation TEXT DEFAULT (date('now'))
    );

    CREATE TABLE IF NOT EXISTS emballages_stock (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type_id INTEGER NOT NULL REFERENCES types_emballages(id),
        depot_id INTEGER REFERENCES depots(id),
        statut TEXT DEFAULT 'disponible',
        quantite INTEGER DEFAULT 0,
        UNIQUE(type_id, depot_id, statut)
    );

    CREATE TABLE IF NOT EXISTS mouvements_emballages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type_id INTEGER NOT NULL REFERENCES types_emballages(id),
        depot_id INTEGER REFERENCES depots(id),
        client_id INTEGER REFERENCES clients(id),
        fournisseur_id INTEGER REFERENCES fournisseurs(id),
        type_mvt TEXT NOT NULL,
        quantite INTEGER NOT NULL DEFAULT 0,
        prix_consigne_unit REAL DEFAULT 0,
        montant_consigne REAL DEFAULT 0,
        statut_avant TEXT,
        statut_apres TEXT,
        doc_type TEXT,
        doc_ref TEXT,
        doc_id INTEGER,
        notes TEXT,
        operateur TEXT,
        date_mvt TEXT DEFAULT (date('now')),
        date_creation TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS consignes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reference TEXT UNIQUE,
        client_id INTEGER NOT NULL REFERENCES clients(id),
        type_emballage_id INTEGER NOT NULL REFERENCES types_emballages(id),
        doc_vente_id INTEGER REFERENCES documents_vente(id),
        date_sortie TEXT DEFAULT (date('now')),
        quantite_sortie INTEGER DEFAULT 0,
        quantite_retournee INTEGER DEFAULT 0,
        prix_consigne_unit REAL DEFAULT 0,
        montant_total REAL DEFAULT 0,
        montant_retourne REAL DEFAULT 0,
        date_retour_prevu TEXT,
        statut TEXT DEFAULT 'en_cours',
        notes TEXT,
        date_creation TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS reparations_emballages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type_id INTEGER NOT NULL REFERENCES types_emballages(id),
        quantite INTEGER DEFAULT 1,
        date_entree TEXT DEFAULT (date('now')),
        date_sortie_prevue TEXT,
        date_sortie_reelle TEXT,
        motif TEXT,
        cout_reparation REAL DEFAULT 0,
        statut TEXT DEFAULT 'en_cours',
        resultat TEXT DEFAULT 'inconnu',
        notes TEXT,
        operateur TEXT,
        date_creation TEXT DEFAULT (datetime('now'))
    );


    CREATE TABLE IF NOT EXISTS ecritures_comptables (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type_ecriture TEXT NOT NULL CHECK(type_ecriture IN ('recette','depense')),
        date_ecriture TEXT NOT NULL DEFAULT (date('now')),
        categorie TEXT NOT NULL,
        libelle TEXT NOT NULL,
        montant REAL NOT NULL DEFAULT 0,
        mode_paiement TEXT DEFAULT 'especes',
        source TEXT,
        source_id INTEGER,
        notes TEXT,
        date_creation TEXT DEFAULT (datetime('now'))
    );


    -- ══ BONS DE LIVRAISON (vente) ══
    CREATE TABLE IF NOT EXISTS bons_livraison (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reference TEXT UNIQUE,
        commande_id INTEGER REFERENCES documents_vente(id),
        client_id INTEGER REFERENCES clients(id),
        depot_id INTEGER REFERENCES depots(id),
        date_bl TEXT DEFAULT (date('now')),
        date_livraison TEXT,
        statut TEXT DEFAULT 'brouillon',
        livreur TEXT,
        notes TEXT,
        total_ht REAL DEFAULT 0,
        total_ttc REAL DEFAULT 0,
        facture_id INTEGER REFERENCES documents_vente(id),
        date_creation TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS lignes_bl (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bl_id INTEGER NOT NULL REFERENCES bons_livraison(id) ON DELETE CASCADE,
        article_id INTEGER REFERENCES articles(id),
        designation TEXT,
        quantite_commandee REAL DEFAULT 0,
        quantite_livree REAL DEFAULT 0,
        quantite_colis REAL DEFAULT 0,
        prix_ht REAL DEFAULT 0,
        tva REAL DEFAULT 0,
        total_ht REAL DEFAULT 0,
        total_ttc REAL DEFAULT 0,
        num_ligne INTEGER DEFAULT 0
    );

    -- ══ AVOIRS CLIENTS ══
    CREATE TABLE IF NOT EXISTS avoirs_clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reference TEXT UNIQUE,
        facture_id INTEGER REFERENCES documents_vente(id),
        client_id INTEGER NOT NULL REFERENCES clients(id),
        date_avoir TEXT DEFAULT (date('now')),
        motif TEXT,
        type_avoir TEXT DEFAULT 'retour',
        total_ht REAL DEFAULT 0,
        total_tva REAL DEFAULT 0,
        total_ttc REAL DEFAULT 0,
        statut TEXT DEFAULT 'en_attente',
        mode_remboursement TEXT DEFAULT 'credit',
        notes TEXT,
        date_creation TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS lignes_avoir_client (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        avoir_id INTEGER NOT NULL REFERENCES avoirs_clients(id) ON DELETE CASCADE,
        article_id INTEGER REFERENCES articles(id),
        designation TEXT,
        quantite REAL DEFAULT 0,
        prix_ht REAL DEFAULT 0,
        tva REAL DEFAULT 0,
        total_ht REAL DEFAULT 0,
        total_ttc REAL DEFAULT 0
    );

    -- ══ FACTURES FOURNISSEURS (distinctes des commandes) ══
    CREATE TABLE IF NOT EXISTS factures_fournisseurs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reference TEXT UNIQUE,
        ref_fournisseur TEXT,
        fournisseur_id INTEGER NOT NULL REFERENCES fournisseurs(id),
        commande_id INTEGER REFERENCES documents_achat(id),
        depot_id INTEGER REFERENCES depots(id),
        date_facture TEXT DEFAULT (date('now')),
        date_echeance TEXT,
        date_reception TEXT,
        statut TEXT DEFAULT 'en_attente',
        total_ht REAL DEFAULT 0,
        total_tva REAL DEFAULT 0,
        total_ttc REAL DEFAULT 0,
        montant_paye REAL DEFAULT 0,
        reste REAL DEFAULT 0,
        mode_paiement TEXT DEFAULT 'virement',
        notes TEXT,
        date_creation TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS lignes_facture_fourn (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        facture_id INTEGER NOT NULL REFERENCES factures_fournisseurs(id) ON DELETE CASCADE,
        article_id INTEGER REFERENCES articles(id),
        designation TEXT,
        quantite REAL DEFAULT 0,
        prix_ht REAL DEFAULT 0,
        tva REAL DEFAULT 0,
        total_ht REAL DEFAULT 0,
        total_ttc REAL DEFAULT 0
    );

    -- ══ AVOIRS FOURNISSEURS ══
    CREATE TABLE IF NOT EXISTS avoirs_fournisseurs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reference TEXT UNIQUE,
        facture_fourn_id INTEGER REFERENCES factures_fournisseurs(id),
        fournisseur_id INTEGER NOT NULL REFERENCES fournisseurs(id),
        date_avoir TEXT DEFAULT (date('now')),
        motif TEXT,
        type_avoir TEXT DEFAULT 'retour',
        total_ht REAL DEFAULT 0,
        total_tva REAL DEFAULT 0,
        total_ttc REAL DEFAULT 0,
        statut TEXT DEFAULT 'en_attente',
        notes TEXT,
        date_creation TEXT DEFAULT (datetime('now'))
    );

    -- ══ RÉSERVATIONS STOCKS ══
    CREATE TABLE IF NOT EXISTS reservations_stock (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        article_id INTEGER NOT NULL REFERENCES articles(id),
        depot_id INTEGER NOT NULL REFERENCES depots(id),
        commande_id INTEGER REFERENCES documents_vente(id),
        bl_id INTEGER REFERENCES bons_livraison(id),
        quantite_reservee REAL DEFAULT 0,
        statut TEXT DEFAULT 'active',
        date_creation TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS cadenciers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER REFERENCES clients(id),
        article_id INTEGER REFERENCES articles(id),
        quantite_habituelle REAL DEFAULT 0,
        frequence TEXT DEFAULT 'hebdomadaire',
        notes TEXT,
        UNIQUE(client_id, article_id)
    );

    CREATE TABLE IF NOT EXISTS representants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        nom TEXT NOT NULL,
        prenom TEXT,
        telephone TEXT,
        email TEXT,
        zone TEXT,
        taux_commission REAL DEFAULT 5.0,
        actif INTEGER DEFAULT 1,
        notes TEXT,
        date_creation TEXT DEFAULT (date('now'))
    );

    CREATE TABLE IF NOT EXISTS relances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL REFERENCES clients(id),
        facture_id INTEGER REFERENCES documents_vente(id),
        date_relance TEXT DEFAULT (date('now')),
        date_echeance TEXT,
        montant_du REAL DEFAULT 0,
        type_relance TEXT DEFAULT 'appel',
        niveau INTEGER DEFAULT 1,
        message TEXT,
        statut TEXT DEFAULT 'planifiee',
        reponse TEXT,
        date_reponse TEXT,
        operateur TEXT,
        date_creation TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS commissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        representant_id INTEGER NOT NULL REFERENCES representants(id),
        document_id INTEGER REFERENCES documents_vente(id),
        client_id INTEGER REFERENCES clients(id),
        date_commission TEXT DEFAULT (date('now')),
        montant_base REAL DEFAULT 0,
        taux REAL DEFAULT 0,
        montant_commission REAL DEFAULT 0,
        statut TEXT DEFAULT 'en_attente',
        date_paiement TEXT,
        notes TEXT,
        date_creation TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS mouvements_compte (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tiers_type TEXT NOT NULL,
        tiers_id INTEGER NOT NULL,
        date_mvt TEXT DEFAULT (date('now')),
        libelle TEXT NOT NULL,
        debit REAL DEFAULT 0,
        credit REAL DEFAULT 0,
        solde_cumule REAL DEFAULT 0,
        doc_type TEXT,
        doc_ref TEXT,
        doc_id INTEGER,
        date_creation TEXT DEFAULT (datetime('now'))
    );

    -- ══ MODULE ATELIER — ÉQUIPEMENTS CLIENTS ══
    CREATE TABLE IF NOT EXISTS equipements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL REFERENCES clients(id),
        type_appareil TEXT NOT NULL DEFAULT 'Autre',
        marque TEXT,
        modele TEXT,
        numero_serie TEXT,
        couleur TEXT,
        description TEXT,
        date_creation TEXT DEFAULT (date('now'))
    );

    -- ══ MODULE ATELIER — TICKETS DE RÉPARATION ══
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reference TEXT UNIQUE,
        client_id INTEGER NOT NULL REFERENCES clients(id),
        equipement_id INTEGER REFERENCES equipements(id),
        type_panne TEXT,
        description_panne TEXT,
        accessoires TEXT,
        mot_de_passe TEXT,
        statut TEXT DEFAULT 'recu',
        priorite TEXT DEFAULT 'normale',
        technicien_id INTEGER REFERENCES employes(id),
        date_reception TEXT DEFAULT (date('now')),
        date_prevue TEXT,
        date_cloture TEXT,
        diagnostic TEXT,
        travaux_effectues TEXT,
        cout_estime REAL DEFAULT 0,
        cout_final REAL DEFAULT 0,
        montant_regle REAL DEFAULT 0,
        notes TEXT,
        date_creation TEXT DEFAULT (datetime('now'))
    );

    -- ══ MODULE ATELIER — RÈGLEMENTS TICKETS ══
    CREATE TABLE IF NOT EXISTS reglements_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
        client_id INTEGER REFERENCES clients(id),
        montant REAL NOT NULL,
        mode_paiement TEXT DEFAULT 'especes',
        date_reglement TEXT DEFAULT (date('now')),
        notes TEXT,
        date_creation TEXT DEFAULT (datetime('now'))
    );
    """)


    # ── Migrations : colonnes ajoutées après création initiale ──
    migrations = [
        ("ALTER TABLE clients ADD COLUMN representant_id INTEGER REFERENCES representants(id)",),
        ("ALTER TABLE documents_vente ADD COLUMN bl_origine_id INTEGER",),
        ("ALTER TABLE documents_achat ADD COLUMN type_doc_achat TEXT DEFAULT 'commande'",),
        ("ALTER TABLE documents_achat ADD COLUMN facture_fourn_id INTEGER",),
        ("ALTER TABLE employes ADD COLUMN actif INTEGER DEFAULT 1",),
        ("ALTER TABLE articles ADD COLUMN prix_unitaire REAL DEFAULT 0",),
        # Enrichissement tiers — classification & contrôle encours
        ("ALTER TABLE clients ADD COLUMN matricule_fiscal TEXT",),
        ("ALTER TABLE clients ADD COLUMN code_comptable TEXT",),
        ("ALTER TABLE clients ADD COLUMN secteur TEXT",),
        ("ALTER TABLE clients ADD COLUMN categorie_client TEXT DEFAULT 'standard'",),
        ("ALTER TABLE clients ADD COLUMN plafond_credit REAL DEFAULT 0",),
        ("ALTER TABLE clients ADD COLUMN telephone_fixe TEXT",),
        ("ALTER TABLE clients ADD COLUMN site_web TEXT",),
        ("ALTER TABLE clients ADD COLUMN responsable_compte TEXT",),
        ("ALTER TABLE fournisseurs ADD COLUMN matricule_fiscal TEXT",),
        ("ALTER TABLE fournisseurs ADD COLUMN code_comptable TEXT",),
        ("ALTER TABLE fournisseurs ADD COLUMN secteur TEXT",),
        ("ALTER TABLE fournisseurs ADD COLUMN categorie_fournisseur TEXT DEFAULT 'standard'",),
        ("ALTER TABLE fournisseurs ADD COLUMN plafond_credit REAL DEFAULT 0",),
        ("ALTER TABLE fournisseurs ADD COLUMN telephone_fixe TEXT",),
        ("ALTER TABLE fournisseurs ADD COLUMN contact2 TEXT",),
        ("ALTER TABLE fournisseurs ADD COLUMN site_web TEXT",),
        ("ALTER TABLE fournisseurs ADD COLUMN telephone2 TEXT",),
        ("ALTER TABLE mouvements_stocks ADD COLUMN stock_apres REAL DEFAULT 0",),
        ("ALTER TABLE articles ADD COLUMN icone TEXT DEFAULT '📦'",),
        ("ALTER TABLE bons_livraison ADD COLUMN total_tva REAL DEFAULT 0",),
        # Devis achat — colonnes supplémentaires
        ("ALTER TABLE documents_achat ADD COLUMN date_validite TEXT",),
        ("ALTER TABLE documents_achat ADD COLUMN objet TEXT",),
        # Remise par ligne sur les bons de livraison (cohérence avec lignes_vente)
        ("ALTER TABLE lignes_bl ADD COLUMN remise_pct REAL DEFAULT 0",),
        # Intégration FNE (Facture Normalisée Électronique — DGI Côte d'Ivoire)
        ("ALTER TABLE documents_vente ADD COLUMN fne_statut TEXT DEFAULT 'non_envoye'",),
        ("ALTER TABLE documents_vente ADD COLUMN fne_reference TEXT",),
        ("ALTER TABLE documents_vente ADD COLUMN fne_invoice_id TEXT",),
        ("ALTER TABLE documents_vente ADD COLUMN fne_qr_token TEXT",),
        ("ALTER TABLE documents_vente ADD COLUMN fne_date_transmission TEXT",),
        ("ALTER TABLE documents_vente ADD COLUMN fne_message_erreur TEXT",),
        ("ALTER TABLE documents_vente ADD COLUMN fne_balance_sticker INTEGER",),
        # Item-level FNE (UUIDs DGI pour les avoirs)
        ("ALTER TABLE lignes_vente ADD COLUMN fne_item_id TEXT",),
        # FNE sur avoirs clients
        ("ALTER TABLE avoirs_clients ADD COLUMN fne_statut TEXT DEFAULT 'non_envoye'",),
        ("ALTER TABLE avoirs_clients ADD COLUMN fne_reference TEXT",),
        ("ALTER TABLE avoirs_clients ADD COLUMN fne_qr_token TEXT",),
        ("ALTER TABLE avoirs_clients ADD COLUMN fne_date_transmission TEXT",),
        ("ALTER TABLE avoirs_clients ADD COLUMN fne_message_erreur TEXT",),
        # Remise par ligne sur BL (pour cohérence avec lignes_vente lors de bl→facture)
        ("ALTER TABLE lignes_bl ADD COLUMN remise_pct REAL DEFAULT 0",),
        # Quantité colis sur BL — transmission commande → BL → facture
        ("ALTER TABLE lignes_bl ADD COLUMN quantite_colis REAL DEFAULT 0",),
        ("ALTER TABLE lignes_facture_fourn ADD COLUMN quantite_unite REAL DEFAULT 0",),
        ("ALTER TABLE lignes_facture_fourn ADD COLUMN quantite_colis REAL DEFAULT 0",),
        # Avances clients — montant déjà imputé sur facture(s)
        ("ALTER TABLE reglements ADD COLUMN avance_imputee REAL DEFAULT 0",),
        ("ALTER TABLE reglements ADD COLUMN motif TEXT DEFAULT ''",),
        ("ALTER TABLE reglements ADD COLUMN commande_id INTEGER",),
        ("ALTER TABLE ecritures_comptables ADD COLUMN motif TEXT DEFAULT ''",),
    ]
    for (sql,) in migrations:
        try:
            db.execute(sql)
        except Exception:
            pass  # Colonne déjà présente
    # Config modules optionnels — INSERT OR IGNORE pour bases existantes
    db.execute("INSERT OR IGNORE INTO parametres(cle,valeur) VALUES('module_caisse','non')")
    db.execute("INSERT OR IGNORE INTO parametres(cle,valeur) VALUES('module_emballages','non')")
    db.execute("INSERT OR IGNORE INTO parametres(cle,valeur) VALUES('module_atelier','non')")

    # ── Migration port 1435 → 1439 ───────────────────────────────────
    # Corrige toute base existante qui aurait conservé l'ancien port
    db.execute("""
        UPDATE parametres SET valeur='1439'
        WHERE cle='server_port' AND valeur='1435'
    """)

    # Données initiales
    cfg_defaults = [
        ('nom_depot', 'MON COMMERCE'),
        ('devise', 'FCFA'),
        ('tva_defaut', '0'),
        ('tva_collectee_taux', '0'),
        ('ville', 'Abidjan'),
        ('telephone', ''),
        ('email', ''),
        ('adresse', ''),
        ('logo', '🛒'),
        # Accès modules — valeur par défaut 'oui' (admin a tout)
        ('acces_devis',          'ecriture'),
        ('acces_commandes',      'ecriture'),
        ('acces_factures',       'ecriture'),
        ('acces_avoirs',         'ecriture'),
        ('acces_achats',         'ecriture'),
        ('acces_factures_fourn', 'ecriture'),
        ('acces_clients',        'ecriture'),
        ('acces_fournisseurs',   'ecriture'),
        ('acces_relances',       'ecriture'),
        ('acces_representants',  'ecriture'),
        ('acces_emballages',     'ecriture'),
        ('module_caisse',        'non'),
        ('module_emballages',    'non'),
        ('module_atelier',       'non'),
        ('acces_atelier_equipements', 'ecriture'),
        ('acces_atelier_tickets',     'ecriture'),
        ('acces_reglements',     'ecriture'),
        ('acces_depenses',       'ecriture'),
        ('acces_comptabilite',   'ecriture'),
        ('acces_articles',       'ecriture'),
        ('acces_familles',       'ecriture'),
        ('acces_unites',         'ecriture'),
        ('acces_stock',          'ecriture'),
        ('acces_depots',         'ecriture'),
        ('acces_employes',       'ecriture'),
        ('acces_paie',           'ecriture'),
        ('acces_conges',         'ecriture'),
        ('acces_caisse',         'ecriture'),
        # Codes de licence pré-générés
        ('code_demo', ''),
        ('code_pro',  'PP-FIB1-PRO-67V016B1'),
        ('code_bus',  'PP-J6HA-BUS-7BOD064E'),
        # Licences pré-activées
        ('code_pro_exp',  '2027-04-17'),
        ('code_bus_exp',  '9999-12-31'),
        # Intégration FNE — DGI Côte d'Ivoire
        # Facture normalisée — URL de redirection
        ('fne_url',           ''),
    ]
    for cle, val in cfg_defaults:
        db.execute("INSERT OR IGNORE INTO parametres(cle,valeur) VALUES(?,?)", (cle, val))

    # ── Pré-activation licences Pro et Business ──────────────────────
    # Ces INSERT OR IGNORE n'écrasent pas si une licence est déjà activée
    _pro_code  = 'PP-FIB1-PRO-67V016B1'
    _bus_code  = 'PP-J6HA-BUS-7BOD064E'
    _pro_exp   = '2027-04-17'
    _bus_exp   = '9999-12-31'
    # Stocker les codes avec leurs expirations
    db.execute("INSERT OR IGNORE INTO parametres(cle,valeur) VALUES('code_pro',?)",       (_pro_code,))
    db.execute("INSERT OR IGNORE INTO parametres(cle,valeur) VALUES('code_pro_exp',?)",   (_pro_exp,))
    db.execute("INSERT OR IGNORE INTO parametres(cle,valeur) VALUES('code_bus',?)",       (_bus_code,))
    db.execute("INSERT OR IGNORE INTO parametres(cle,valeur) VALUES('code_bus_exp',?)",   (_bus_exp,))
    # Licences stockées — activation manuelle via /activation requise

    # Admin par défaut
    db.execute("""
        INSERT OR IGNORE INTO utilisateurs(nom,prenom,email,mot_de_passe,role)
        VALUES('Admin','Système','admin','admin123',  'admin')
    """)

    # Familles articles
    familles_default = [
        ('ALIM',   'Alimentation', '#f59e0b'),
        ('HYGIENE','Hygiène & Beauté', '#10b981'),
        ('MAISON', 'Maison & Entretien', '#3b82f6'),
        ('TEXTILE','Textile & Vêtements', '#8b5cf6'),
        ('ELECT',  'Électronique', '#f97316'),
        ('PAPETER','Papeterie & Bureau', '#06b6d4'),
        ('AUTRES', 'Autres produits', '#6b7280'),
    ]
    for code, nom, couleur in familles_default:
        db.execute("INSERT OR IGNORE INTO familles(code,nom,couleur) VALUES(?,?,?)", (code,nom,couleur))

    # Unités de vente par défaut
    unites_default = ['Bouteille','Canette','Carton','Caisse','Pack','Palette','Unité','Litre','Sachet','Fût']
    for u in unites_default:
        db.execute("INSERT OR IGNORE INTO unites_vente(nom) VALUES(?)", (u,))

    # Dépôt principal
    db.execute("INSERT OR IGNORE INTO depots(code,nom,adresse) VALUES('DEP01','Dépôt Principal','Abidjan')")

    # ── Client par défaut (passager / caisse) ── CLI000 ─────────────────
    # Ce client est utilisé pour toutes les ventes sans client identifié.
    # Il est créé une seule fois et n'est jamais supprimé (actif=1 forcé).
    db.execute("""
        INSERT OR IGNORE INTO clients(code, nom, prenom, type_client, telephone,
            ville, encours_autorise, remise_pct, mode_paiement, delai_paiement,
            actif, notes)
        VALUES('CLI000', 'CLIENT PASSAGER', 'Caisse', 'particulier', '',
               'Abidjan', 0, 0, 'especes', 0, 1,
               'Client par défaut — utilisé pour les ventes comptoir sans identification.')
    """)

    # Types emballages par défaut
    types_emb = [
        ('EMB001','Bouteille 33cl',  'bouteille','33 cl', 150, 50, 60,50,'#f59e0b'),
        ('EMB002','Bouteille 65cl',  'bouteille','65 cl', 200, 80, 60,50,'#f97316'),
        ('EMB003','Bouteille 1L',    'bouteille','1 L',   250,100, 60,50,'#ef4444'),
        ('EMB004','Casier 12 btes',  'casier',   '12 b',  600,400,120,100,'#3b82f6'),
        ('EMB005','Casier 24 btes',  'casier',   '24 b',  800,600,120,100,'#6366f1'),
        ('EMB006','Fût 30L',         'fut',      '30 L', 5000,3000,240,200,'#8b5cf6'),
        ('EMB007','Fût 50L',         'fut',      '50 L', 7500,5000,240,200,'#7c3aed'),
        ('EMB008','Bonbonne 5L',     'bonbonne', '5 L',   800,500, 60, 30,'#10b981'),
        ('EMB009','Bonbonne 20L',    'bonbonne', '20 L', 2000,1500,60, 30,'#059669'),
    ]
    for code,nom,cat,cont,pcons,pachat,duree,rotmax,coul in types_emb:
        db.execute("""INSERT OR IGNORE INTO types_emballages
                       (code,nom,categorie,contenance,prix_consigne,prix_achat,
                        duree_vie_mois,nb_rotations_max,couleur)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                   (code,nom,cat,cont,pcons,pachat,duree,rotmax,coul))

    # -- Migration : username pour les DB existantes --
    try:
        db.execute('ALTER TABLE utilisateurs ADD COLUMN username TEXT')
        db.commit()
    except Exception:
        pass  # colonne deja presente
    try:
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_utilisateurs_username ON utilisateurs(username)')
        db.commit()
    except Exception:
        pass
    try:
        db.execute('ALTER TABLE utilisateurs ADD COLUMN prenom TEXT')
        db.commit()
    except Exception:
        pass

    db.commit()
    db.close()

# ══════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════
def get_cfg():
    rows = query("SELECT cle, valeur FROM parametres")
    return {r['cle']: r['valeur'] for r in rows}


def _get_perm(module_key):
    """Retourne la permission de l'utilisateur connecté pour un module.
    Valeurs possibles : 'ecriture', 'lecture', 'non'.
    L'admin a toujours 'ecriture'.
    Supporte les rôles CSV multi-valeurs (ex: 'commercial,caissier').
    """
    role_raw = session.get('user_role', '')
    roles = [r.strip() for r in role_raw.split(',') if r.strip()]
    if 'admin' in roles:
        return 'ecriture'
    slot = session.get('user_slot', '')
    if not slot:
        return 'non'
    cfg = get_cfg()
    raw = cfg.get(slot + '_' + module_key, 'ecriture')
    # rétrocompat
    if raw == 'oui':
        return 'ecriture'
    if raw in ('non', 'lecture', 'ecriture'):
        return raw
    return 'ecriture'


def can_read(module_key):
    """True si l'utilisateur peut au moins lire ce module."""
    return _get_perm(module_key) in ('lecture', 'ecriture')


def can_write(module_key):
    """True si l'utilisateur peut écrire dans ce module."""
    return _get_perm(module_key) == 'ecriture'


def _licence_valide():
    """Retourne True si une licence active et non expirée existe en DB."""
    try:
        cfg  = get_cfg()
        plan = cfg.get('licence_plan', '')
        exp  = cfg.get('licence_expiration', '')
        if not plan or not exp:
            return False
        if plan == 'business':
            return True
        return date.fromisoformat(exp) >= date.today()
    except Exception:
        return False


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # 1 — Licence valide ?
        if not _licence_valide():
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'error': 'licence_requise', 'redirect': '/activation'}), 403
            return redirect(url_for('activation'))
        # 2 — Session active ?
        if not session.get('user_id'):
            if request.path.startswith('/api/') or \
               request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'error': 'session_expiree', 'redirect': '/login'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════════════════
#  SYSTÈME DE PERMISSIONS GLOBAL
# ══════════════════════════════════════════════════════════════════════
# Mapping URL → module. Ordre important : la première règle qui matche gagne.
# Les préfixes plus spécifiques DOIVENT venir avant les plus génériques.
_PERMISSION_RULES = [
    # ── Préfixes spécifiques d'abord ──
    ('/avoirs_fournisseurs',     'acces_factures_fourn'),
    ('/api/achats',              'acces_achats'),
    ('/api/caisse',              'acces_caisse'),
    ('/catalogue/familles',      'acces_familles'),
    ('/catalogue/unites',        'acces_unites'),
    ('/catalogue',               'acces_articles'),
    ('/commandes_vente',         'acces_commandes'),
    ('/commissions/payer',       'acces_representants'),
    ('/consignes/retour',        'acces_emballages'),
    ('/documents_vente',         'acces_factures'),
    ('/ventes/regler',           'acces_factures'),
    ('/comptabilite',            'acces_comptabilite'),
    ('/ecritures',               'acces_comptabilite'),
    ('/bons_livraison',          'acces_factures'),
    ('/stock/inventaire',        'acces_stock'),
    ('/inventaire',              'acces_stock'),
    # ── Préfixes génériques ──
    ('/caisse',                  'acces_caisse'),
    ('/devis',                   'acces_devis'),
    ('/commandes',               'acces_commandes'),
    ('/factures',                'acces_factures'),
    ('/avoirs',                  'acces_avoirs'),
    ('/achats',                  'acces_achats'),
    ('/clients',                 'acces_clients'),
    ('/fournisseurs',            'acces_fournisseurs'),
    ('/relances',                'acces_relances'),
    ('/representants',           'acces_representants'),
    ('/emballages',              'acces_emballages'),
    ('/equipements',             'acces_atelier_equipements'),
    ('/tickets',                 'acces_atelier_tickets'),
    ('/reglements',              'acces_reglements'),
    ('/depenses',                'acces_depenses'),
    ('/articles',                'acces_articles'),
    ('/familles',                'acces_familles'),
    ('/unites_vente',            'acces_unites'),
    ('/stock',                   'acces_stock'),
    ('/depots',                  'acces_depots'),
    ('/employes',                'acces_employes'),
    ('/paie',                    'acces_paie'),
    ('/conges',                  'acces_conges'),
]

# Chemins exemptés du contrôle de permission (toujours accessibles si connecté)
_EXEMPT_PATHS = (
    '/', '/login', '/logout', '/dashboard', '/splash',
    '/activation',
    '/parametres',                # déjà protégé par check admin
    '/utilisateurs',              # admin only via parametres
    '/api/search', '/search',     # recherche globale
    '/api/alertes',
    '/api/articles',              # lookup générique
    '/api/clients',
    '/api/facture',
    '/api/stock',
    '/api/doc_vente',
    '/api/emballages',
    '/api/equipements',
    '/static',
)


def _module_for_path(path):
    """Retourne la clé module pour une URL, ou None si pas de restriction."""
    for prefix, module in _PERMISSION_RULES:
        if path == prefix or path.startswith(prefix + '/') or path.startswith(prefix + '?'):
            return module
    return None


def _is_exempt(path):
    """True si le chemin n'est pas soumis au contrôle de permission."""
    for ex in _EXEMPT_PATHS:
        if path == ex or path.startswith(ex + '/'):
            return True
    return False


def _is_api_path(path):
    return path.startswith('/api/')


@app.before_request
def _enforce_permissions():
    """Hook global : vérifie les permissions du module avant chaque requête."""
    path = request.path

    # 1. Routes exemptées
    if _is_exempt(path):
        return None

    # 2. Pas de session = laisser login_required gérer
    if not session.get('user_id'):
        return None

    # 3. Admin = bypass total
    if session.get('user_role') == 'admin':
        return None

    # 4. Identifier le module
    module = _module_for_path(path)
    if not module:
        return None  # route non régulée → libre accès (mais session requise via login_required)

    # 5. Vérifier la permission
    is_write = request.method in ('POST', 'PUT', 'PATCH', 'DELETE')
    perm = _get_perm(module)

    refused = False
    raison = ""
    if perm == 'non':
        refused = True
        raison = "Vous n'avez pas accès à ce module."
    elif is_write and perm == 'lecture':
        refused = True
        raison = "Vous avez un accès en lecture seule sur ce module."

    if refused:
        if _is_api_path(path) or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'error': 'permission_refusee', 'module': module, 'message': raison}), 403
        flash(f"🚫 {raison}", "danger")
        return redirect(url_for('dashboard'))

    return None


# ── Helpers exposés à Jinja2 (pour cacher/désactiver les liens UI) ──
@app.context_processor
def _inject_perm_helpers():
    """Rend can_read/can_write disponibles dans tous les templates."""
    # cfg injecté globalement pour que base.html ait toujours accès aux
    # réglages d'apparence (ex. menu_mode). Une valeur cfg= passée
    # explicitement à render_template reste prioritaire sur celle-ci.
    try:
        _cfg_global = get_cfg()
    except Exception:
        _cfg_global = {}
    return {
        'can_read':  can_read,
        'can_write': can_write,
        'is_admin':  (session.get('user_role') == 'admin'),
        'current_role': session.get('user_role', ''),
        'current_slot': session.get('user_slot', ''),
        'cfg': _cfg_global,
    }


def _next_ref_seq(table, prefix):
    """Génère une référence séquentielle anti-collision : <prefix><yymm><NNN>.
    Robuste face aux formats hétérogènes (références importées comme
    'RGL-2605-0015', séquences dépassant 999, trous) : on calcule le plus
    grand numéro réellement utilisé, puis on cherche le premier numéro LIBRE.
    'table' provient toujours du code (jamais de l'utilisateur)."""
    base = f"{prefix}{date.today().strftime('%y%m')}"
    rows = query(f"SELECT reference FROM {table} WHERE reference LIKE ?", (base + '%',))
    max_num = 0
    for r in rows:
        suffixe = (r['reference'] or '')[len(base):]
        digits = ''.join(ch for ch in suffixe if ch.isdigit())
        if digits:
            try:
                max_num = max(max_num, int(digits))
            except ValueError:
                pass
    num = max_num + 1
    while query(f"SELECT 1 FROM {table} WHERE reference=?", (f"{base}{num:03d}",), one=True):
        num += 1
    return f"{base}{num:03d}"


def next_ref(prefix):
    return _next_ref_seq('documents_vente', prefix)

def next_ref_achat(prefix):
    return _next_ref_seq('documents_achat', prefix)

def next_ref_rgl():
    return _next_ref_seq('reglements', 'RGL')

def next_ref_bl():
    return _next_ref_seq('bons_livraison', 'BL')

def fcfa(val):
    try:
        v = float(val or 0)
        return f"{v:,.0f}".replace(',', '·')
    except:
        return "0"

app.jinja_env.filters['fcfa'] = fcfa

# ══════════════════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════
#  MOTEUR DE LICENCES DISTRIGEST
# ══════════════════════════════════════════════════════════════════════
_SALT = 'STINAUGTECH2025PP'

# Codes fixes DISTRIGEST
_CODES_VALIDES = {
    'PP-FIB1-PRO-67V016B1': 'pro',
    'PP-J6HA-BUS-7BOD064E': 'business',
}

def valider_code_licence(code):
    code = code.strip().upper()
    plan = _CODES_VALIDES.get(code)
    if not plan:
        return {'valide': False, 'plan': None, 'message': 'Code invalide ou incorrect'}
    ids = {'pro': 'PRO', 'business': 'BUS'}
    return {'valide': True, 'plan': plan, 'plan_id': ids[plan], 'message': 'Code valide'}

def get_licence_info():
    cfg    = get_cfg()
    plan   = cfg.get('licence_plan','')
    exp    = cfg.get('licence_expiration','')
    code   = cfg.get('licence_code','')
    labels = {'demo':'Demo — 5 jours','pro':'Pro — 12 mois','business':'Business illimité'}
    ids    = {'demo':'DEMO','pro':'PRO','business':'BUS'}
    if not plan or not exp:
        return None
    info = {'plan_id':ids.get(plan,''),'plan_nom':labels.get(plan,plan),'code':code,
            'etat':'actif','illimitee':plan=='business',
            'date_expiration':exp,'jours_restants':None,'pct_restant':100}
    if plan != 'business':
        try:
            from datetime import date as _d
            delta = (_d.fromisoformat(exp) - _d.today()).days
            duree = 365 if plan=='pro' else 5
            info['jours_restants'] = max(0, delta)
            info['pct_restant']    = max(0, min(100, int(delta/duree*100)))
            info['etat']           = 'expire' if delta < 0 else 'actif'
        except Exception:
            pass
    return info


@app.route('/activation')
def activation():
    # Si licence déjà active et non expirée → ne plus afficher cette page
    lic = get_licence_info()
    if lic and lic.get('etat') == 'actif':
        return redirect(url_for('login'))
    codes = None
    if session.get('user_role') == 'admin':
        cfg   = get_cfg()
        codes = {'DEMO':cfg.get('code_demo',''),'PRO':cfg.get('code_pro',''),'BUS':cfg.get('code_bus','')}
    return render_template('activation.html', licence_active=lic, codes=codes, code_error=None)


@app.route('/activation/demo', methods=['POST'])
def activation_demo():
    exp = (date.today() + timedelta(days=5)).isoformat()
    execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES('licence_plan',?)",       ('demo',))
    execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES('licence_code',?)",       ('DEMO-GRATUIT',))
    execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES('licence_expiration',?)", (exp,))
    execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES('licence_activee_le',?)", (date.today().isoformat(),))
    flash(f"✅ Essai gratuit activé — 5 jours jusqu'au {exp}.", 'success')
    return redirect(url_for('dashboard'))


@app.route('/activation/valider', methods=['POST'])
def activation_valider():
    code   = request.form.get('code_activation','').strip()
    result = valider_code_licence(code)
    if not result['valide']:
        return render_template('activation.html', licence_active=get_licence_info(),
                               codes=None, code_error=result['message'])
    plan = result['plan']
    exps = {'demo':(date.today()+timedelta(days=5)).isoformat(),
            'pro':(date.today()+timedelta(days=365)).isoformat(),
            'business':'9999-12-31'}
    execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES('licence_plan',?)",       (plan,))
    execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES('licence_code',?)",       (code,))
    execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES('licence_expiration',?)", (exps[plan],))
    execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES('licence_activee_le',?)", (date.today().isoformat(),))
    labels = {'demo':'Demo (5 jours)','pro':'Pro (12 mois)','business':'Business (illimité)'}
    flash(f"✅ Licence {labels[plan]} activée jusqu\'au {exps[plan]}.", 'success')
    return redirect(url_for('dashboard'))


@app.route('/activation/generer/<plan>')
@login_required
def activation_generer(plan):
    if session.get('user_role') != 'admin':
        return jsonify({'error': 'admin only'}), 403
    codes = {'pro': 'PP-FIB1-PRO-67V016B1', 'business': 'PP-J6HA-BUS-7BOD064E'}
    code = codes.get(plan, '')
    return jsonify({'code': code, 'plan': plan})


@app.route('/')
def splash():
    cfg = get_cfg()
    plan = cfg.get('licence_plan', '')
    exp  = cfg.get('licence_expiration', '')
    # Licence valide et non expirée → login
    if plan and exp:
        try:
            from datetime import date as _d
            if plan == 'business' or _d.fromisoformat(exp) >= _d.today():
                return render_template('demarrage.html', redirect_to='/login')
        except Exception:
            pass
    # Pas de licence ou expirée → activation
    return render_template('demarrage.html', redirect_to='/activation')


@app.route('/login', methods=['GET', 'POST'])
def login():
    # Pas de licence → activation obligatoire
    if not _licence_valide():
        return redirect(url_for('activation'))
    error = None
    if request.method == 'POST':
        identifier = request.form.get('email', '').strip()
        password   = request.form.get('password', '').strip()
        user = query("""SELECT * FROM utilisateurs WHERE (email=? OR nom=? OR username=?) AND mot_de_passe=? AND actif=1""",
                     (identifier, identifier, identifier, password), one=True)
        if user:
            session['user_id']   = user['id']
            session['user_nom']  = user['nom']
            # Stocker le rôle principal (issu de utilisateurs.role)
            session['user_role'] = user['role']
            # Charger la chaîne CSV complète depuis parametres (multi-rôles)
            slot_tmp = 'admin' if user['role'] == 'admin' else (user['prenom'] or '')
            if slot_tmp and slot_tmp != 'admin':
                from sqlite3 import connect as _sc
                _cfg_row = query("SELECT valeur FROM parametres WHERE cle=?",
                                 (slot_tmp + '_role',), one=True)
                if _cfg_row and _cfg_row['valeur']:
                    session['user_role'] = _cfg_row['valeur']  # CSV complet
            # ── Déterminer le slot pour le système de permissions ──
            if user['role'] == 'admin':
                session['user_slot'] = 'admin'
            else:
                # Le slot ('user_1', 'user_2', ...) est stocké dans le champ prenom (convention interne)
                session['user_slot'] = user['prenom'] if user['prenom'] else ''
            return redirect(url_for('dashboard'))
        error = "Identifiant ou mot de passe incorrect."
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ══════════════════════════════════════════════════════════════════════
#  DASHBOARD
# ══════════════════════════════════════════════════════════════════════
@app.route('/')
@app.route('/dashboard')
@login_required
def dashboard():
    cfg = get_cfg()
    today = date.today()
    mois_debut = today.replace(day=1).isoformat()

    # KPI
    nb_clients     = query("SELECT COUNT(*) as n FROM clients WHERE actif=1", one=True)['n']
    nb_articles    = query("SELECT COUNT(*) as n FROM articles WHERE actif=1", one=True)['n']
    nb_fournisseurs= query("SELECT COUNT(*) as n FROM fournisseurs WHERE actif=1", one=True)['n']

    ca_mois = query("""SELECT COALESCE(SUM(total_ttc),0) as s FROM documents_vente
                       WHERE type_doc='facture' AND date_doc>=? AND statut!='annule'""",
                    (mois_debut,), one=True)['s']
    # Déduire les avoirs clients du mois (réductions de recettes en temps réel)
    avoirs_clients_mois = query("""SELECT COALESCE(SUM(total_ttc),0) as s FROM avoirs_clients
                                   WHERE date_avoir>=?""", (mois_debut,), one=True)['s']
    ca_mois = max(0, ca_mois - avoirs_clients_mois)

    ca_encours = query("""SELECT COALESCE(SUM(reste),0) as s FROM documents_vente
                          WHERE type_doc='facture' AND statut IN ('en_attente','partielle')""",
                       one=True)['s']

    nb_cmde_vente = query("""SELECT COUNT(*) as n FROM documents_vente
                             WHERE type_doc='commande' AND statut IN ('en_attente','confirme')""",
                          one=True)['n']

    nb_cmde_achat = query("""SELECT COUNT(*) as n FROM documents_achat
                             WHERE type_doc='commande' AND statut IN ('en_attente','confirme')""",
                          one=True)['n']

    stock_alerte = query("""SELECT COUNT(*) as n FROM stocks s
                            JOIN articles a ON a.id=s.article_id
                            WHERE s.quantite_unite <= s.stock_min_unite AND s.stock_min_unite > 0""",
                         one=True)['n']

    depenses_mois = query("""SELECT COALESCE(SUM(montant),0) as s FROM depenses
                             WHERE date_depense>=?""", (mois_debut,), one=True)['s']
    # Déduire les avoirs fournisseurs du mois (réductions de dépenses en temps réel)
    avoirs_fourn_mois = query("""SELECT COALESCE(SUM(total_ttc),0) as s FROM avoirs_fournisseurs
                                 WHERE date_avoir>=?""", (mois_debut,), one=True)['s']
    depenses_mois = max(0, depenses_mois - avoirs_fourn_mois)

    # Factures récentes
    factures_recentes = query("""
        SELECT dv.*, c.nom as client_nom, c.telephone as client_tel
        FROM documents_vente dv
        LEFT JOIN clients c ON c.id=dv.client_id
        WHERE dv.type_doc='facture'
        ORDER BY dv.date_creation DESC LIMIT 8
    """)

    # Alertes stock
    alertes = query("""
        SELECT a.designation, a.reference, s.quantite_unite, s.stock_min_unite, d.nom as depot
        FROM stocks s
        JOIN articles a ON a.id=s.article_id
        JOIN depots d ON d.id=s.depot_id
        WHERE s.quantite_unite <= s.stock_min_unite AND s.stock_min_unite > 0
        ORDER BY (s.quantite_unite - s.stock_min_unite) ASC LIMIT 5
    """)

    # Évolution CA 6 mois
    ca_par_mois = []
    for i in range(5, -1, -1):
        d = (today.replace(day=1) - timedelta(days=i*30))
        m_debut = d.replace(day=1).isoformat()
        if d.month == 12:
            m_fin = d.replace(year=d.year+1, month=1, day=1).isoformat()
        else:
            m_fin = d.replace(month=d.month+1, day=1).isoformat()
        r = query("""SELECT COALESCE(SUM(total_ttc),0) as s FROM documents_vente
                     WHERE type_doc='facture' AND date_doc>=? AND date_doc<? AND statut!='annule'""",
                  (m_debut, m_fin), one=True)
        mois_label = ['Jan','Fév','Mar','Avr','Mai','Jun','Jul','Aoû','Sep','Oct','Nov','Déc'][d.month-1]
        ca_par_mois.append({'mois': mois_label, 'ca': r['s'] if r else 0})

    return render_template('dashboard.html',
        cfg=cfg, today=today.isoformat(),
        nb_clients=nb_clients, nb_articles=nb_articles,
        nb_fournisseurs=nb_fournisseurs,
        ca_mois=ca_mois, ca_encours=ca_encours,
        nb_cmde_vente=nb_cmde_vente, nb_cmde_achat=nb_cmde_achat,
        stock_alerte=stock_alerte, depenses_mois=depenses_mois,
        factures_recentes=factures_recentes, alertes=alertes,
        ca_par_mois=ca_par_mois
    )

# ══════════════════════════════════════════════════════════════════════
#  CAISSE / POS  (Point de vente tactile)
# ══════════════════════════════════════════════════════════════════════
@app.route('/caisse')
@login_required
def caisse():
    cfg = get_cfg()
    if cfg.get('module_caisse') != 'oui':
        flash("Le module Caisse n'est pas activé. Activez-le dans Paramètres.", "warning")
        return redirect(url_for('dashboard'))
    # Articles actifs vendables à l'unité (prix_unitaire > 0) avec famille
    # Stock TOTAL EN UNITÉS = colis × colisage + unités libres (tous dépôts).
    # Les articles sont stockés en colis mais vendus en caisse à l'unité.
    articles = query("""
        SELECT a.id, a.reference, a.designation, a.icone,
               a.prix_unitaire, a.prix_vente_ht, a.tva, a.code_barre,
               a.colisage, a.unite_vente,
               f.nom AS famille_nom, f.couleur AS famille_couleur,
               COALESCE((
                   SELECT SUM(COALESCE(s.quantite_colis,0) * COALESCE(a.colisage,1)
                            + COALESCE(s.quantite_unite,0))
                   FROM stocks s WHERE s.article_id=a.id
               ), 0) AS stock_total
        FROM articles a
        LEFT JOIN familles f ON f.id=a.famille_id
        WHERE a.actif=1 AND COALESCE(a.prix_unitaire,0) > 0
        ORDER BY f.ordre, f.nom, a.designation
    """)
    # Liste familles distinctes (pour générer les onglets)
    familles = query("SELECT id, nom, couleur FROM familles ORDER BY ordre, nom")
    # Premier dépôt actif (les ventes POS y seront imputées par défaut)
    depot = query("SELECT id, nom FROM depots WHERE actif=1 ORDER BY id LIMIT 1", one=True)
    # TVA caisse : on prend tva_collectee_taux puis tva_defaut en repli
    tva_caisse = float(cfg.get('tva_collectee_taux', cfg.get('tva_defaut', 0)) or 0)
    # Liste clients actifs pour le select POS (CLI000 passager en tête)
    clients_pos = query("""
        SELECT id, code, nom, prenom
        FROM clients WHERE actif=1
        ORDER BY CASE WHEN code='CLI000' THEN 0 ELSE 1 END, nom
    """)
    return render_template('caisse.html', cfg=cfg,
                           articles=articles, familles=familles,
                           depot=depot, tva_caisse=tva_caisse,
                           clients_pos=clients_pos)


# ══════════════════════════════════════════════════════════════════════
#  RÈGLES MÉTIER VENTE — Validation centralisée
# ══════════════════════════════════════════════════════════════════════
def _valider_regles_vente(lignes, type_doc, depot_id):
    """
    Vérifie les règles métier de vente sur une liste de lignes :

      R-STOCK : la quantité demandée d'une ligne ne doit pas dépasser
                le stock disponible. Vérification effectuée LIGNE PAR LIGNE
                indépendamment — pas de cumul entre lignes du même article
                (chaque ligne peut représenter un mouvement différent).
                Appliquée uniquement aux types qui consomment réellement
                du stock : 'facture', 'livraison', 'pos'.
                Le stock total prend en compte le déballage automatique
                des colis vers les unités (stock_total_u = qu + qc * colisage).

      R-PRIX  : le prix de vente unitaire HT doit être >= prix d'achat.
                Appliquee aux types devis/commande/facture/livraison.
                NON appliquee au type 'pos' : le prix_achat_ht peut etre
                saisi a l'unite ou au colis sans champ discriminant,
                ce qui rendrait la comparaison non fiable en caisse.
                Si prix_achat = 0 en BDD, la regle est desactivee.

    Paramètres :
      lignes     : liste de dict ; chaque ligne attend au minimum
                   article_id, et selon le cas qte_unite / qte_colis /
                   quantite, prix_ht (ou prix).
      type_doc   : 'devis' | 'commande' | 'facture' | 'livraison' | 'pos'
      depot_id   : id du dépôt concerné (pour la vérification stock)

    Retourne :
      list[str] : messages d'erreur. Vide si tout est conforme.
    """
    errors = []
    # Lignes sans article (saisie libre type "Frais divers") → on ne valide rien
    art_ids = [int(l.get('article_id')) for l in lignes if l.get('article_id')]
    if not art_ids:
        return errors
    ph = ','.join('?' * len(art_ids))

    # Charger les articles concernés en un seul SELECT
    arts_rows = query(
        f"""SELECT id, designation, prix_achat_ht AS prix_achat, COALESCE(colisage,1) AS colisage
            FROM articles WHERE id IN ({ph})""",
        tuple(art_ids))
    articles_idx = {a['id']: a for a in arts_rows}

    # Charger les stocks du dépôt (uniquement pour les types qui consomment du stock)
    stocks_idx = {}
    if depot_id and type_doc in ('facture', 'livraison', 'pos'):
        stk_rows = query(
            f"""SELECT article_id,
                       COALESCE(quantite_unite,0) AS qu,
                       COALESCE(quantite_colis,0) AS qc
                FROM stocks
                WHERE depot_id=? AND article_id IN ({ph})""",
            (depot_id,) + tuple(art_ids))
        stocks_idx = {s['article_id']: s for s in stk_rows}

    # ── R-STOCK : vérifier chaque ligne indépendamment contre le stock dispo
    #    (pas de cumul des lignes du même article — chaque ligne est traitée seule)
    if type_doc in ('facture', 'livraison', 'pos'):
        for idx, l in enumerate(lignes, 1):
            aid = l.get('article_id')
            if not aid:
                continue
            aid = int(aid)
            art = articles_idx.get(aid)
            if not art:
                continue
            colisage = max(1, int(art['colisage'] or 1))
            qte_u = float(l.get('qte_unite', l.get('quantite', 0)) or 0)
            qte_c = float(l.get('qte_colis', l.get('colis', 0)) or 0)
            # Si seul le colis est saisi, convertir en unités
            if qte_c > 0 and qte_u == 0:
                qte_u = qte_c * colisage
            if qte_u <= 0:
                continue
            s = stocks_idx.get(aid)
            qu = float(s['qu']) if s else 0.0
            qc = float(s['qc']) if s else 0.0
            stock_total_u = qu + qc * colisage  # avec déballage automatique
            if qte_u > stock_total_u:
                manque = qte_u - stock_total_u
                errors.append(
                    f"⛔ Ligne {idx} — Stock insuffisant pour « {art['designation']} » : "
                    f"demandé {int(qte_u)} u, disponible {int(stock_total_u)} u "
                    f"(manque {int(manque)} u)."
                )

    # ── R-PRIX : prix de vente unitaire HT ≥ prix d'achat UNITAIRE (par ligne)
    #    Désactivée pour la caisse POS : le prix_achat_ht peut être saisi
    #    à l'unité OU au colis selon l'article, sans champ discriminant en BDD.
    #    La vérification s'effectue en amont via la fiche article.
    for l in (lignes if type_doc != 'pos' else []):
        aid = l.get('article_id')
        if not aid:
            continue
        aid = int(aid)
        art = articles_idx.get(aid)
        if not art:
            continue
        prix_achat_brut = float(art['prix_achat'] or 0)
        if prix_achat_brut <= 0:
            continue  # règle désactivée si pas de coût de revient connu
        colisage = max(1, int(art['colisage'] or 1))
        # Ramener le prix d'achat à l'unité (cas où il est saisi au colis)
        prix_achat_u = prix_achat_brut / colisage
        prix_vente = float(l.get('prix_ht', l.get('prix', 0)) or 0)
        if 0 < prix_vente < prix_achat_u:
            errors.append(
                f"⛔ « {art['designation']} » : prix de vente "
                f"({int(prix_vente):,} ".replace(',', ' ')
                + f"FCFA) inférieur au prix d'achat unitaire "
                f"({int(prix_achat_u):,} ".replace(',', ' ')
                + "FCFA) — non autorisé."
            )

    return errors


def _restituer_stock_doc_vente(doc_id, doc_ref='', doc_types=('facture','livraison','pos','vente'),
                               notes_prefix="Annulation", tracer=True):
    """Restitue le stock pour un document vente — inverse les mouvements 'sortie'
    existants liés à ce document.

    Pour chaque mouvement 'sortie', réinjecte la quantité en `stocks.quantite_unite`
    (le déballage colis → unités n'est pas réversible physiquement, donc on rend
    en unités libres). Si tracer=True, insère également un mouvement 'entree'
    compensatoire pour la traçabilité comptable.

    Retourne la liste des mouvements anciens (pour permettre un rollback éventuel).
    """
    old_mvts = query("""SELECT id, article_id, depot_id, quantite_unite, prix_unitaire
                        FROM mouvements_stocks
                        WHERE doc_id=? AND type_mvt='sortie'
                          AND doc_type IN ({ph})""".format(
                            ph=','.join('?' * len(doc_types))),
                     (doc_id,) + tuple(doc_types))
    nb = 0
    for m in old_mvts:
        aid = m['article_id']; did = m['depot_id']
        qte = float(m['quantite_unite'] or 0)
        if not (aid and did and qte > 0):
            continue
        execute("""INSERT INTO stocks(article_id, depot_id, quantite_unite, quantite_colis)
                   VALUES(?,?,?,0) ON CONFLICT(article_id,depot_id)
                   DO UPDATE SET quantite_unite = quantite_unite + ?""",
                (aid, did, qte, qte))
        if tracer:
            st = query("""SELECT COALESCE(s.quantite_unite,0) AS qu,
                                 COALESCE(s.quantite_colis,0) AS qc,
                                 COALESCE(a.colisage,1)       AS col
                          FROM stocks s LEFT JOIN articles a ON a.id = s.article_id
                          WHERE s.article_id=? AND s.depot_id=?""", (aid, did), one=True)
            stock_apres = (st['qu'] + st['qc'] * st['col']) if st else qte
            execute("""INSERT INTO mouvements_stocks
                       (article_id, depot_id, type_mvt, quantite_unite, prix_unitaire,
                        doc_type, doc_id, doc_ref, stock_apres, operateur, notes)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (aid, did, 'entree', qte, float(m['prix_unitaire'] or 0),
                     'annulation', doc_id, doc_ref or '', stock_apres,
                     session.get('user_nom') or session.get('user') or 'SYSTEM',
                     f"{notes_prefix} {doc_ref} — retour stock"))
        nb += 1
    return list(old_mvts), nb


def _decrementer_stock_doc_vente(lignes, dep_id, doc_type, doc_id, doc_ref):
    """Décrémente le stock pour les lignes d'un document vente, avec déballage
    automatique colis → unités si les unités libres sont insuffisantes.
    Insère les mouvements_stocks 'sortie' correspondants.

    Attend que chaque ligne expose `_qte_u` (calculé en amont) ou `qte_unite`.
    """
    if not dep_id:
        return
    for l in lignes:
        aid = l.get('article_id')
        if not aid:
            continue
        qte = float(l.get('_qte_u', l.get('qte_unite', 0)) or 0)
        if qte <= 0:
            continue
        art_info = query("SELECT COALESCE(colisage,1) AS colisage FROM articles WHERE id=?",
                         (aid,), one=True)
        colisage = max(int(art_info['colisage'] or 1), 1) if art_info else 1
        st_row = query("""SELECT COALESCE(quantite_unite,0) AS qu,
                                 COALESCE(quantite_colis,0) AS qc
                          FROM stocks WHERE article_id=? AND depot_id=?""",
                       (aid, dep_id), one=True)
        qu = float(st_row['qu']) if st_row else 0.0
        qc = float(st_row['qc']) if st_row else 0.0
        # Déballage automatique si unités libres insuffisantes
        if qte > qu and colisage > 0:
            manquant       = qte - qu
            colis_a_ouvrir = int((manquant + colisage - 1) // colisage)
            colis_a_ouvrir = min(colis_a_ouvrir, int(qc))
            qu += colis_a_ouvrir * colisage
            qc -= colis_a_ouvrir
        qu_apres = qu - qte
        if st_row:
            execute("""UPDATE stocks SET quantite_unite=?, quantite_colis=?
                       WHERE article_id=? AND depot_id=?""",
                    (qu_apres, qc, aid, dep_id))
        else:
            execute("""INSERT INTO stocks(article_id, depot_id, quantite_unite, quantite_colis)
                       VALUES(?,?,?,?)""", (aid, dep_id, qu_apres, qc))
        stock_total_apres = qu_apres + qc * colisage
        execute("""INSERT INTO mouvements_stocks
                   (article_id, depot_id, type_mvt, quantite_unite, prix_unitaire,
                    stock_apres, doc_type, doc_id, doc_ref, operateur)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (aid, dep_id, 'sortie', qte, float(l.get('prix_ht', 0)),
                 stock_total_apres, doc_type, doc_id, doc_ref,
                 session.get('user_nom') or session.get('user') or doc_type.upper()))


@app.route('/caisse/encaisser', methods=['POST'])
@login_required
def caisse_encaisser():
    """Enregistre une vente POS (statut 'payee' ou 'attente')."""
    import json as _json
    data = request.get_json(silent=True) or {}
    items     = data.get('items') or []
    if not items:
        return jsonify(ok=False, error="Panier vide"), 400
    statut    = (data.get('statut') or 'payee').strip()
    statut    = 'reglee' if statut == 'payee' else 'en_attente'
    mode      = data.get('mode') or 'especes'
    remise    = float(data.get('remise_pct') or 0)
    client_id = data.get('client_id') or None
    if client_id:
        try:
            client_id = int(client_id)
        except (ValueError, TypeError):
            client_id = None
    # Récupérer le nom pour la note (sécurité : depuis BDD)
    client_nm = ''
    if client_id:
        c = query("SELECT nom, prenom, code FROM clients WHERE id=? AND actif=1",
                  (client_id,), one=True)
        if c:
            client_nm = c['nom'] + ((' ' + c['prenom']) if c['prenom'] else '')
            # CLI000 = passager → on conserve le client_id pour traçabilité fiche client
            if c['code'] == 'CLI000':
                client_nm = 'Passager'
    cfg       = get_cfg()

    # Dépôt : 1er actif
    depot = query("SELECT id FROM depots WHERE actif=1 ORDER BY id LIMIT 1", one=True)
    depot_id = depot['id'] if depot else None

    # Calcul totaux (les prix POS sont TTC unitaires, on récupère le prix réel en BDD pour sécu)
    total_ttc = 0.0
    rows = []
    for it in items:
        art = query("SELECT id, designation, prix_unitaire, tva FROM articles WHERE id=? AND actif=1",
                    (it.get('id'),), one=True)
        if not art or not (art['prix_unitaire'] or 0) > 0:
            continue
        qty = float(it.get('qty') or 0)
        if qty <= 0:
            continue
        # Prix TTC unitaire envoyé par le front (sécurité : on revérifie en BDD)
        pu_ttc = float(it.get('prix') or art['prix_unitaire'] or 0)
        tva = float(art['tva'] or cfg.get('tva_defaut', 0) or 0)
        ligne_ttc = pu_ttc * qty
        ligne_ht  = ligne_ttc / (1 + tva/100.0) if tva else ligne_ttc
        total_ttc += ligne_ttc
        rows.append((art['id'], art['designation'], qty, pu_ttc, tva, ligne_ht, ligne_ttc))

    if not rows:
        return jsonify(ok=False, error="Aucun article valide"), 400

    # ── Règles métier (stock + prix de vente ≥ prix d'achat) ───────────
    lignes_compat = []
    for (aid, des, qty, pu_ttc, tva_r, l_ht, l_ttc) in rows:
        pu_ht = (pu_ttc / (1 + float(tva_r) / 100.0)) if tva_r else pu_ttc
        lignes_compat.append({
            'article_id': aid,
            'qte_unite' : qty,
            'prix_ht'   : pu_ht,
        })
    errs = _valider_regles_vente(lignes_compat, 'pos', depot_id)
    if errs:
        return jsonify(ok=False, error=' · '.join(errs)), 400

    # Application remise globale
    remise_mt = total_ttc * remise / 100.0
    ttc_final = total_ttc - remise_mt
    ht_final  = sum(r[5] for r in rows) * (1 - remise/100.0)
    tva_final = ttc_final - ht_final

    # ── Mode modification : editing_id fourni par le front ──────────────
    editing_id = data.get('editing_id') or None
    if editing_id:
        try:
            editing_id = int(editing_id)
        except (ValueError, TypeError):
            editing_id = None

    if editing_id:
        # Vérifier que la vente existe et est bien en_attente
        old_doc = query("SELECT id, reference, statut, depot_id FROM documents_vente WHERE id=? AND type_doc='facture'",
                        (editing_id,), one=True)
        if not old_doc or old_doc['statut'] != 'en_attente':
            return jsonify(ok=False, error="Vente introuvable ou déjà encaissée — rechargez la page"), 409

        ref    = old_doc['reference']   # on garde la même référence
        doc_id = editing_id
        edit_depot_id = old_doc['depot_id'] or depot_id

        # ── 1. Réintégrer le stock des anciennes lignes ──────────────────
        old_lignes = query("SELECT article_id, quantite_unite FROM lignes_vente WHERE document_id=?",
                           (doc_id,))
        for ol in (old_lignes or []):
            aid_old = ol['article_id']
            qty_old = float(ol['quantite_unite'] or 0)
            if qty_old <= 0 or not edit_depot_id:
                continue
            art_info = query("SELECT COALESCE(colisage,1) AS colisage FROM articles WHERE id=?",
                             (aid_old,), one=True)
            colisage_old = max(int(art_info['colisage'] if art_info else 1), 1)
            st = query("SELECT COALESCE(quantite_unite,0) AS qu, COALESCE(quantite_colis,0) AS qc FROM stocks WHERE article_id=? AND depot_id=?",
                       (aid_old, edit_depot_id), one=True)
            qu_old = float(st['qu']) if st else 0.0
            qc_old = float(st['qc']) if st else 0.0
            qu_restituee = qu_old + qty_old
            execute("UPDATE stocks SET quantite_unite=? WHERE article_id=? AND depot_id=?",
                    (qu_restituee, aid_old, edit_depot_id))
            execute("""INSERT INTO mouvements_stocks
                       (article_id, depot_id, type_mvt, quantite_unite, prix_unitaire,
                        doc_type, doc_id, doc_ref, stock_apres, operateur)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (aid_old, edit_depot_id, 'annulation_pos', qty_old, 0,
                     'pos', doc_id, ref,
                     qu_restituee + qc_old * colisage_old,
                     session.get('user') or 'POS'))

        # ── 2. Supprimer les anciennes lignes de vente ───────────────────
        execute("DELETE FROM lignes_vente WHERE document_id=?", (doc_id,))

        # ── 3. Mettre à jour l'entête du document ────────────────────────
        execute("""UPDATE documents_vente SET
                       client_id=?, statut=?, remise_globale=?,
                       total_ht=?, total_tva=?, total_ttc=?,
                       montant_paye=?, reste=?, mode_paiement=?,
                       notes=?
                   WHERE id=?""",
                (client_id, statut,
                 remise, ht_final, tva_final, ttc_final,
                 ttc_final if statut == 'reglee' else 0,
                 0        if statut == 'reglee' else ttc_final,
                 mode,
                 ('POS — ' + client_nm) if client_nm else 'POS',
                 doc_id))

    else:
        # ── Création d'un nouveau document ──────────────────────────────
        ref = next_ref('FA')
        execute("""INSERT INTO documents_vente
                   (type_doc, reference, client_id, depot_id, date_doc, statut,
                    remise_globale, total_ht, total_tva, total_ttc,
                    montant_paye, reste, mode_paiement, notes)
                   VALUES('facture',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ref, client_id, depot_id, date.today().isoformat(), statut,
                 remise, ht_final, tva_final, ttc_final,
                 ttc_final if statut == 'reglee' else 0,
                 0        if statut == 'reglee' else ttc_final,
                 mode,
                 ('POS — ' + client_nm) if client_nm else 'POS'))
        doc = query("SELECT id FROM documents_vente WHERE reference=?", (ref,), one=True)
        doc_id = doc['id']

    # ── Nouvelles lignes + mouvements stock (sortie) ─────────────────────
    # Logique stock : la caisse vend à l'unité, mais l'article est stocké
    # majoritairement en colis. Si stock unité libre insuffisant, on
    # « déballe » automatiquement les colis nécessaires.
    for i,(aid, des, qty, pu_ttc, tva, l_ht, l_ttc) in enumerate(rows, 1):
        pu_ht = pu_ttc / (1 + tva/100.0) if tva else pu_ttc
        execute("""INSERT INTO lignes_vente
                   (document_id, article_id, designation, quantite_unite,
                    prix_ht, tva, total_ht, total_ttc, num_ligne)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (doc_id, aid, des, qty, pu_ht, tva, l_ht, l_ttc, i))
        # Décrément stock : déballage colis → unités si nécessaire
        if depot_id:
            art_info = query("SELECT COALESCE(colisage,1) AS colisage FROM articles WHERE id=?",
                             (aid,), one=True)
            colisage = max(int(art_info['colisage'] or 1), 1)
            st_row = query("SELECT COALESCE(quantite_unite,0) AS qu, COALESCE(quantite_colis,0) AS qc FROM stocks WHERE article_id=? AND depot_id=?",
                           (aid, depot_id), one=True)
            qu = float(st_row['qu']) if st_row else 0.0
            qc = float(st_row['qc']) if st_row else 0.0
            besoin = float(qty)
            # Si pas assez d'unités libres, déballer des colis
            if besoin > qu and colisage > 0:
                manquant       = besoin - qu
                colis_a_ouvrir = int((manquant + colisage - 1) // colisage)  # arrondi sup
                colis_a_ouvrir = min(colis_a_ouvrir, int(qc))                # plafonné par stock dispo
                qu += colis_a_ouvrir * colisage
                qc -= colis_a_ouvrir
            # Sortie effective
            qu_apres = qu - besoin
            execute("UPDATE stocks SET quantite_unite=?, quantite_colis=? WHERE article_id=? AND depot_id=?",
                    (qu_apres, qc, aid, depot_id))
            execute("""INSERT INTO mouvements_stocks
                       (article_id, depot_id, type_mvt, quantite_unite, prix_unitaire,
                        doc_type, doc_id, doc_ref, stock_apres, operateur)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (aid, depot_id, 'sortie', qty, pu_ttc, 'pos', doc_id, ref,
                     qu_apres + qc * colisage,
                     session.get('user') or 'POS'))

    # Règlement immédiat si payée
    if statut == 'reglee':
        rgl_ref = next_ref_rgl()
        execute("""INSERT INTO reglements(reference, source_type, source_id, client_id, montant,
                   mode_paiement, date_reglement, notes)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (rgl_ref, 'vente', doc_id, client_id, ttc_final, mode,
                 date.today().isoformat(), f'Règlement caisse — {ref}'))

    # ── Ouverture automatique du tiroir-caisse (vente espèces réglée) ──
    # Non bloquant : un échec d'ouverture ne doit jamais faire échouer la vente.
    tiroir_ouvert = False
    try:
        if (statut == 'reglee'
                and (mode or '').lower().strip() in ('especes', 'cash')
                and cfg.get('tiroir_caisse') == 'oui'
                and cfg.get('tiroir_auto', 'oui') == 'oui'):
            _t_ok, _t_err = _ouvrir_tiroir_caisse()
            tiroir_ouvert = bool(_t_ok)
            if not _t_ok:
                logging.warning("[TIROIR] Auto-ouverture échouée (%s) : %s", ref, _t_err)
    except Exception as _exc_t:
        logging.warning("[TIROIR] Auto-ouverture exception : %s", _exc_t)

    # Infos imprimante pour l'impression automatique côté front
    print_auto = cfg.get('imprimante_recu_auto', 'non') == 'oui'
    print_type = cfg.get('imprimante_type', 'a4')

    return jsonify(ok=True, id=doc_id, ref=ref, total=ttc_final, statut=statut,
                   print_auto=print_auto, print_type=print_type,
                   tiroir=tiroir_ouvert,
                   impression_mode=_impression_mode())

@app.route('/caisse/ventes')
@login_required
def caisse_ventes():
    """Retourne les ventes POS (origine = notes commençant par 'POS')
       pour alimenter l'onglet Historique."""
    rows = query("""
        SELECT dv.id, dv.reference, dv.date_creation, dv.date_doc,
               dv.total_ttc, dv.remise_globale, dv.mode_paiement,
               dv.statut, dv.notes, dv.reste, dv.montant_paye
        FROM documents_vente dv
        WHERE dv.type_doc='facture' AND (dv.notes LIKE 'POS%')
        ORDER BY dv.date_creation DESC
        LIMIT 200
    """)
    out = []
    for r in rows:
        # Statut frontend : reglee→payee, partielle→partielle, en_attente→attente
        st_map = {'reglee':'payee', 'partielle':'partielle', 'en_attente':'attente', 'annule':'annulee'}
        client_nm = ''
        if r['notes'] and r['notes'].startswith('POS — '):
            client_nm = r['notes'][6:].strip()
        out.append({
            'id'    : r['id'],
            'ref'   : r['reference'],
            'date'  : (r['date_creation'] or r['date_doc'] or ''),
            'client': client_nm,
            'total' : float(r['total_ttc'] or 0),
            'remise': float(r['remise_globale'] or 0),
            'mode'  : r['mode_paiement'] or 'especes',
            'statut': st_map.get(r['statut'], r['statut']),
        })
    return jsonify(ventes=out)


@app.route('/caisse/ventes/<int:doc_id>')
@login_required
def caisse_vente_lignes(doc_id):
    """Retourne les lignes d'articles d'une vente POS.
       Utilisé pour l'impression du reçu ET pour la reprise en modification."""
    doc    = query("SELECT statut, remise_globale, mode_paiement, notes, client_id FROM documents_vente WHERE id=?",
                   (doc_id,), one=True)
    lignes = query("""
        SELECT lv.article_id,
               lv.quantite_unite          AS quantite,
               lv.prix_ht                 AS prix_unitaire_ht,
               lv.tva,
               lv.total_ttc               AS total,
               COALESCE(a.colisage, 1)    AS colisage,
               COALESCE(a.prix_unitaire, lv.prix_ht) AS prix_ttc_ref,
               COALESCE(lv.designation, a.designation, 'Article') AS nom,
               COALESCE(a.icone, '📦')    AS ico,
               COALESCE((
                   SELECT SUM(COALESCE(s.quantite_colis,0) * COALESCE(a.colisage,1)
                            + COALESCE(s.quantite_unite,0))
                   FROM stocks s WHERE s.article_id = a.id
               ), 0) AS stock
        FROM lignes_vente lv
        LEFT JOIN articles a ON a.id = lv.article_id
        WHERE lv.document_id = ?
        ORDER BY lv.num_ligne
    """, (doc_id,))
    out = []
    for l in lignes:
        tva_r  = float(l['tva'] or 0)
        pu_ht  = float(l['prix_unitaire_ht'] or 0)
        pu_ttc = round(pu_ht * (1 + tva_r / 100.0), 2) if tva_r else pu_ht
        out.append({
            'nom'           : l['nom'],
            'quantite'      : float(l['quantite'] or 1),
            'prix_unitaire' : pu_ht,
            'prix_ttc'      : pu_ttc,
            'total'         : float(l['total'] or 0),
            'article_id'    : l['article_id'],
            'colisage'      : int(l['colisage'] or 1),
            'ico'           : l['ico'],
            'stock'         : float(l['stock'] or 0),
        })
    meta = {}
    if doc:
        meta = {
            'remise'    : float(doc['remise_globale'] or 0),
            'mode'      : doc['mode_paiement'] or 'especes',
            'client_id' : doc['client_id'],
            'statut'    : doc['statut'],
        }
    return jsonify(lignes=out, meta=meta)



@app.route('/caisse/regler/<int:doc_id>', methods=['POST'])
@login_required
def caisse_regler(doc_id):
    """Regle une vente POS en attente depuis la caisse (JSON)."""
    data = request.get_json(silent=True) or {}
    doc = query("SELECT * FROM documents_vente WHERE id=?", (doc_id,), one=True)
    if not doc:
        return jsonify(ok=False, error="Vente introuvable"), 404
    if doc["statut"] not in ("en_attente", "partielle"):
        return jsonify(ok=False, error="Cette vente n'est pas en attente"), 400
    montant = float(data.get("montant") or doc["total_ttc"] or 0)
    if montant <= 0:
        return jsonify(ok=False, error="Montant invalide"), 400
    mode = data.get("mode_paiement") or data.get("mode") or "especes"
    ref  = next_ref_rgl()
    nouveau_paye = round((doc["montant_paye"] or 0) + montant, 2)
    reste        = round(max(0, (doc["total_ttc"] or 0) - nouveau_paye), 2)
    statut       = "reglee" if reste <= 0 else "partielle"
    execute("UPDATE documents_vente SET montant_paye=?, reste=?, statut=?, mode_paiement=? WHERE id=?",
            (nouveau_paye, reste, statut, mode, doc_id))
    execute("""INSERT INTO reglements(reference, source_type, source_id, client_id, montant, mode_paiement, date_reglement)
               VALUES(?,?,?,?,?,?,date("now"))""",
            (ref, "facture", doc_id, doc["client_id"], round(montant, 2), mode))
    return jsonify(ok=True, ref=ref, statut=statut, reste=reste)

# ══════════════════════════════════════════════════════════════════════
#  ARTICLES
# ══════════════════════════════════════════════════════════════════════
@app.route('/articles')
@login_required
def articles_list():
    cfg = get_cfg()
    q = request.args.get('q', '')
    famille_f = request.args.get('famille', '')
    sql = """SELECT a.*, f.nom as famille_nom, f.couleur as famille_couleur
             FROM articles a LEFT JOIN familles f ON f.id=a.famille_id
             WHERE a.actif=1 """
    args = []
    if q:
        sql += " AND (a.designation LIKE ? OR a.reference LIKE ? OR a.code_barre LIKE ?)"
        args += [f'%{q}%', f'%{q}%', f'%{q}%']
    if famille_f:
        sql += " AND a.famille_id=?"
        args.append(famille_f)
    sql += " ORDER BY a.designation"
    articles = query(sql, args)
    familles = query("SELECT * FROM familles ORDER BY nom")
    unites   = query("SELECT nom FROM unites_vente WHERE actif=1 ORDER BY nom")
    tva_def  = float(cfg.get('tva_collectee_taux', cfg.get('tva', 0)) or 0)
    return render_template('articles.html', cfg=cfg, articles=articles,
                           familles=familles, unites=unites,
                           tva_collectee=tva_def,
                           q=q, famille_f=famille_f)

@app.route('/articles/add', methods=['POST'])
@login_required
def article_add():
    f = request.form
    # Auto-référence
    ref = f.get('reference','').strip()
    if not ref:
        count = query("SELECT COUNT(*) as n FROM articles", one=True)['n']
        ref = f"ART{count+1:04d}"
    # TVA par défaut depuis la configuration (champ supprimé du formulaire)
    tva_def = float(get_cfg().get('tva_defaut', 0) or 0)
    try:
        execute("""INSERT INTO articles(reference,designation,famille_id,contenance,colisage,
                   unite_vente,prix_achat_ht,prix_vente_ht,prix_unitaire,tva,code_barre,notes,icone)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ref, f['designation'], f.get('famille_id') or None,
                 f.get('contenance'), int(f.get('colisage',1) or 1),
                 f.get('unite_vente','Unité'),
                 float(f.get('prix_achat_ht',0) or 0),
                 float(f.get('prix_vente_ht',0) or 0),
                 float(f.get('prix_unitaire',0) or 0),
                 tva_def,
                 f.get('code_barre'),
                 f.get('notes'),
                 f.get('icone') or '📦'))
    except sqlite3.IntegrityError:
        flash(f"❌ La référence « {ref} » existe déjà. Veuillez utiliser une référence différente.", "danger")
        return redirect(url_for('articles_list'))
    # Créer ligne stock dans tous les dépôts
    art_id = query("SELECT id FROM articles WHERE reference=?", (ref,), one=True)['id']
    depots = query("SELECT id FROM depots WHERE actif=1")
    for d in depots:
        execute("INSERT OR IGNORE INTO stocks(article_id,depot_id) VALUES(?,?)", (art_id, d['id']))
    flash("Article ajouté avec succès.", "success")
    return redirect(url_for('articles_list'))

@app.route('/articles/delete/<int:id>')
@login_required
def article_delete(id):
    execute("UPDATE articles SET actif=0 WHERE id=?", (id,))
    flash("Article supprimé.", "success")
    return redirect(url_for('articles_list'))

@app.route('/articles/edit/<int:id>', methods=['POST'])
@login_required
def article_edit(id):
    f = request.form
    ref = f.get('reference', '').strip()
    if not ref:
        existing = query("SELECT reference FROM articles WHERE id=?", (id,), one=True)
        ref = existing['reference'] if existing else f"ART{id:04d}"
    try:
        execute("""UPDATE articles SET
                   reference=?, designation=?, famille_id=?, contenance=?,
                   colisage=?, unite_vente=?, prix_achat_ht=?, prix_vente_ht=?,
                   prix_unitaire=?, tva=?, code_barre=?, icone=?
                   WHERE id=?""",
                (ref, f['designation'], f.get('famille_id') or None,
                 f.get('contenance'),
                 int(f.get('colisage', 1) or 1),
                 f.get('unite_vente', 'Unité'),
                 float(f.get('prix_achat_ht', 0) or 0),
                 float(f.get('prix_vente_ht', 0) or 0),
                 float(f.get('prix_unitaire', 0) or 0),
                 float(f.get('tva', 0) or 0),
                 f.get('code_barre') or None,
                 f.get('icone') or '📦',
                 id))
    except sqlite3.IntegrityError:
        flash(f"❌ La référence « {ref} » est déjà utilisée par un autre article.", "danger")
        return redirect(url_for('articles_list'))
    flash("Article modifié avec succès.", "success")
    return redirect(url_for('articles_list'))


# ══════════════════════════════════════════════════════════════════════
#  CATALOGUE — Familles & Unités de vente
# ══════════════════════════════════════════════════════════════════════

@app.route('/catalogue')
@login_required
def catalogue_list():
    cfg = get_cfg()
    familles   = query("SELECT *, (SELECT COUNT(*) FROM articles WHERE famille_id=familles.id AND actif=1) as nb_articles FROM familles ORDER BY ordre, nom")
    unites     = query("SELECT *, (SELECT COUNT(*) FROM articles WHERE unite_vente=unites_vente.nom AND actif=1) as nb_articles FROM unites_vente WHERE actif=1 ORDER BY nom")
    return render_template('catalogue.html', cfg=cfg, familles=familles, unites=unites)


@app.route('/familles')
@login_required
def familles_list():
    cfg = get_cfg()
    familles = query("SELECT *, (SELECT COUNT(*) FROM articles WHERE famille_id=familles.id AND actif=1) as nb_articles FROM familles ORDER BY ordre, nom")
    return render_template('familles_articles.html', cfg=cfg, familles=familles)


@app.route('/unites_vente')
@login_required
def unites_vente_list():
    cfg = get_cfg()
    unites = query("SELECT *, (SELECT COUNT(*) FROM articles WHERE unite_vente=unites_vente.nom AND actif=1) as nb_articles FROM unites_vente WHERE actif=1 ORDER BY nom")
    return render_template('unites_vente.html', cfg=cfg, unites=unites)


# ── Familles ─────────────────────────────────────────────────────────
@app.route('/catalogue/familles/add', methods=['POST'])
@login_required
def famille_add():
    f = request.form
    nom = f.get('nom','').strip()
    if not nom:
        flash("Le nom est requis.", "danger")
        return redirect(url_for('catalogue_list'))
    code = f.get('code','').strip().upper() or nom[:6].upper().replace(' ','_')
    execute("INSERT OR IGNORE INTO familles(code,nom,couleur,ordre) VALUES(?,?,?,?)",
            (code, nom, f.get('couleur','#2563eb'), int(f.get('ordre',0) or 0)))
    flash(f"Famille « {nom} » ajoutée.", "success")
    return redirect(url_for('familles_list'))


@app.route('/catalogue/familles/edit/<int:id>', methods=['POST'])
@login_required
def famille_edit(id):
    f = request.form
    execute("UPDATE familles SET nom=?, couleur=?, ordre=? WHERE id=?",
            (f['nom'], f.get('couleur','#2563eb'), int(f.get('ordre',0) or 0), id))
    flash("Famille mise à jour.", "success")
    return redirect(url_for('familles_list'))


@app.route('/catalogue/familles/delete/<int:id>')
@login_required
def famille_delete(id):
    nb = query("SELECT COUNT(*) as n FROM articles WHERE famille_id=? AND actif=1", (id,), one=True)['n']
    if nb > 0:
        flash(f"Impossible : {nb} article(s) utilisent cette famille.", "danger")
        return redirect(url_for('catalogue_list'))
    execute("DELETE FROM familles WHERE id=?", (id,))
    flash("Famille supprimée.", "success")
    return redirect(url_for('familles_list'))


# ── Unités de vente ───────────────────────────────────────────────────
@app.route('/catalogue/unites/add', methods=['POST'])
@login_required
def unite_add():
    nom = request.form.get('nom','').strip()
    if not nom:
        flash("Le nom est requis.", "danger")
        return redirect(url_for('unites_vente_list'))
    execute("INSERT OR IGNORE INTO unites_vente(nom) VALUES(?)", (nom,))
    flash(f"Unité « {nom} » ajoutée.", "success")
    return redirect(url_for('unites_vente_list'))


@app.route('/catalogue/unites/edit/<int:id>', methods=['POST'])
@login_required
def unite_edit(id):
    nom = request.form.get('nom','').strip()
    if not nom:
        flash("Le nom est requis.", "danger")
        return redirect(url_for('unites_vente_list'))
    execute("UPDATE unites_vente SET nom=? WHERE id=?", (nom, id))
    flash(f"Unité mise à jour.", "success")
    return redirect(url_for('unites_vente_list'))


@app.route('/catalogue/unites/delete/<int:id>')
@login_required
def unite_delete(id):
    u = query("SELECT nom FROM unites_vente WHERE id=?", (id,), one=True)
    if u:
        nb = query("SELECT COUNT(*) as n FROM articles WHERE unite_vente=? AND actif=1", (u['nom'],), one=True)['n']
        if nb > 0:
            flash(f"Impossible : {nb} article(s) utilisent cette unité.", "danger")
            return redirect(url_for('unites_vente_list'))
    execute("DELETE FROM unites_vente WHERE id=?", (id,))
    flash("Unité supprimée.", "success")
    return redirect(url_for('unites_vente_list'))


# API articles — catalogue F4 enrichi
@app.route('/api/articles')
@login_required
def api_articles():
    q = request.args.get('q', '')
    depot_id = int(request.args.get('depot_id', 1) or 1)
    cat = request.args.get('cat', '')
    rows = query("""
        SELECT a.id, a.reference, a.designation,
               a.prix_vente_ht, a.prix_achat_ht, a.prix_vente_ht as prix_vente,
               a.prix_unitaire,
               a.tva, a.colisage, a.unite_vente, a.contenance,
               COALESCE(f.nom,'') as categorie,
               COALESCE((
                   SELECT SUM(COALESCE(s2.quantite_unite,0)
                            + COALESCE(s2.quantite_colis,0) * COALESCE(a.colisage,1))
                   FROM stocks s2 WHERE s2.article_id=a.id
               ), 0) as quantite,
               COALESCE(s.stock_min_unite, 0) as quantite_min
        FROM articles a
        LEFT JOIN stocks s ON s.article_id=a.id AND s.depot_id=?
        LEFT JOIN familles f ON f.id=a.famille_id
        WHERE a.actif=1
          AND (a.designation LIKE ? OR a.reference LIKE ?)
          AND (? = '' OR f.nom = ?)
        ORDER BY a.designation LIMIT 100
    """, (depot_id, f'%{q}%', f'%{q}%', cat, cat))
    articles = [dict(r) for r in rows]
    # Fallback prix_unitaire : dériver de prix_vente_ht / colisage si non défini
    for a in articles:
        if not a.get('prix_unitaire'):
            col = max(1, int(a.get('colisage') or 1))
            a['prix_unitaire'] = round((a.get('prix_vente_ht') or 0) / col) if col > 1 else (a.get('prix_vente_ht') or 0)
    # Catégories distinctes
    cats = sorted(set(a['categorie'] for a in articles if a['categorie']))
    return jsonify({'articles': articles, 'cats': cats})

@app.route('/api/caisse/stock')
@login_required
def api_caisse_stock():
    """Stock temps réel pour la Caisse/POS — renvoie {article_id: stock_total_unites}.
    Calcule le stock total tous dépôts (colis×colisage + unités libres), identique
    à la logique de la page /caisse. Appelée en polling JS toutes les 10s.
    """
    rows = query("""
        SELECT a.id,
               COALESCE((
                   SELECT SUM(COALESCE(s.quantite_colis,0) * COALESCE(a.colisage,1)
                            + COALESCE(s.quantite_unite,0))
                   FROM stocks s WHERE s.article_id=a.id
               ), 0) AS stock_total
        FROM articles a
        WHERE a.actif=1 AND COALESCE(a.prix_unitaire,0) > 0
    """)
    return jsonify({r['id']: r['stock_total'] for r in rows})

@app.route('/api/facture/stock')
@login_required
def api_facture_stock():
    """Stock temps réel pour le module Factures — renvoie {article_id: stock_total_unites}.
    Supporte le filtrage par dépôt (?depot_id=N). Appelée en polling JS toutes les 15s.
    Identique à /api/caisse/stock mais filtrée par dépôt si spécifié.
    """
    depot_id = request.args.get('depot_id')
    if depot_id:
        try:
            depot_id = int(depot_id)
        except (ValueError, TypeError):
            depot_id = None
    if depot_id:
        rows = query("""
            SELECT a.id,
                   COALESCE(
                       COALESCE(s.quantite_colis,0) * COALESCE(a.colisage,1)
                       + COALESCE(s.quantite_unite,0)
                   , 0) AS stock_total
            FROM articles a
            LEFT JOIN stocks s ON s.article_id=a.id AND s.depot_id=?
            WHERE a.actif=1
        """, (depot_id,))
    else:
        rows = query("""
            SELECT a.id,
                   COALESCE((
                       SELECT SUM(COALESCE(s.quantite_colis,0) * COALESCE(a.colisage,1)
                                + COALESCE(s.quantite_unite,0))
                       FROM stocks s WHERE s.article_id=a.id
                   ), 0) AS stock_total
            FROM articles a
            WHERE a.actif=1
        """)
    return jsonify({r['id']: r['stock_total'] for r in rows})

# ══════════════════════════════════════════════════════════════════════
#  DÉPÔTS
# ══════════════════════════════════════════════════════════════════════
@app.route('/depots')
@login_required
def depots_list():
    cfg = get_cfg()
    depots = query("""
        SELECT d.*, COUNT(DISTINCT s.article_id) as nb_articles,
               COALESCE(SUM(
                 CASE
                   WHEN COALESCE(a.prix_vente_ht,0) > 0
                     -- colis : nb_colis × prix_vente_colis
                     THEN (s.quantite_unite / COALESCE(a.colisage,1)) * a.prix_vente_ht
                   ELSE
                     -- unitaire : quantite_unite × prix_unitaire
                     s.quantite_unite * COALESCE(a.prix_unitaire,0)
                 END
               ),0) as valeur_stock
        FROM depots d
        LEFT JOIN stocks s ON s.depot_id=d.id AND s.quantite_unite > 0
        LEFT JOIN articles a ON a.id=s.article_id
        GROUP BY d.id ORDER BY d.code
    """)
    return render_template('depots.html', cfg=cfg, depots=depots)

@app.route('/depots/add', methods=['POST'])
@login_required
def depot_add():
    f = request.form
    code = f.get('code','').strip().upper()
    if not code:
        n = query("SELECT COUNT(*) as n FROM depots", one=True)['n']
        code = f"DEP{n+1:02d}"
    execute("INSERT INTO depots(code,nom,adresse,responsable) VALUES(?,?,?,?)",
            (code, f['nom'], f.get('adresse'), f.get('responsable')))
    dep_id = query("SELECT id FROM depots WHERE code=?", (code,), one=True)['id']
    arts = query("SELECT id FROM articles WHERE actif=1")
    for a in arts:
        execute("INSERT OR IGNORE INTO stocks(article_id,depot_id) VALUES(?,?)", (a['id'], dep_id))
    flash("Dépôt créé.", "success")
    return redirect(url_for('depots_list'))

@app.route('/depots/edit/<int:id>', methods=['POST'])
@login_required
def depot_edit(id):
    f = request.form
    execute("UPDATE depots SET nom=?,adresse=?,responsable=? WHERE id=?",
            (f['nom'], f.get('adresse'), f.get('responsable'), id))
    flash("Dépôt modifié.", "success")
    return redirect(url_for('depots_list'))

@app.route('/depots/delete/<int:id>')
@login_required
def depot_delete(id):
    execute("UPDATE depots SET actif=0 WHERE id=?", (id,))
    flash("Dépôt désactivé.", "success")
    return redirect(url_for('depots_list'))

# ══════════════════════════════════════════════════════════════════════
#  STOCK
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/stock/live')
@login_required
def api_stock_live():
    """API temps réel — renvoie l'état actuel des stocks + derniers mouvements.

    Optimisation : si le paramètre `since=<id>` est fourni et qu'aucun
    mouvement n'est plus récent que cet ID, renvoie HTTP 204 No Content
    (≈ 50 octets) pour économiser bande passante et charge BDD.
    Sinon, renvoie un snapshot JSON complet (filtré par dépôt + recherche
    si paramètres fournis, identique à la page /stock).

    Curseur : MAX(id) de mouvements_stocks (auto-increment strictement
    croissant) — fiable car chaque entrée/sortie crée une ligne.
    """
    since = int(request.args.get('since', 0) or 0)
    last  = query("SELECT COALESCE(MAX(id), 0) AS m FROM mouvements_stocks", one=True)
    cursor = int(last['m']) if last else 0

    # Rien de neuf depuis le dernier check → 204 (économe)
    if since > 0 and cursor <= since:
        return ('', 204)

    # Filtres (mêmes que la page /stock pour cohérence)
    depot_id = request.args.get('depot', '')
    q        = request.args.get('q', '')

    sql = """SELECT s.article_id, s.depot_id,
                    COALESCE(s.quantite_unite, 0) AS qu,
                    COALESCE(s.quantite_colis, 0) AS qc,
                    COALESCE(s.stock_min_unite, 0) AS smin,
                    COALESCE(a.colisage, 1) AS colisage,
                    COALESCE(a.prix_vente_ht, 0) AS prix_vente_ht,
                    COALESCE(a.prix_unitaire, 0) AS prix_unitaire
             FROM stocks s
             JOIN articles a ON a.id = s.article_id
             JOIN depots   d ON d.id = s.depot_id
             WHERE a.actif = 1 AND d.actif = 1"""
    args = []
    if depot_id:
        sql += " AND s.depot_id = ?"
        args.append(depot_id)
    if q:
        sql += " AND (a.designation LIKE ? OR a.reference LIKE ?)"
        args += [f'%{q}%', f'%{q}%']

    rows_s = query(sql, args)
    stocks = []
    valeur_totale = 0
    nb_alertes = nb_ruptures = 0
    for s in rows_s:
        qu = float(s['qu'])
        smin = float(s['smin'])
        # Règle valeur : colis × prix_colis  OU  unités × prix_unitaire
        pv_ht = float(s['prix_vente_ht'])
        pu    = float(s['prix_unitaire'])
        colisage = max(1, int(s['colisage']))
        nb_col = qu / colisage if colisage > 0 else 0
        if pv_ht > 0:
            val = int(nb_col * pv_ht)
        else:
            val = int(qu * pu)
        rupture = qu <= 0
        alerte  = qu <= smin and smin > 0 and not rupture
        if rupture: nb_ruptures += 1
        if alerte:  nb_alertes += 1
        valeur_totale += val
        stocks.append({
            'article_id': s['article_id'],
            'depot_id'  : s['depot_id'],
            'qu'      : int(qu),
            'qc_calc' : int(qu / colisage) if colisage > 0 else 0,
            'smin'    : int(smin),
            'rupture' : rupture,
            'alerte'  : alerte,
            'valeur'  : val,
        })

    # 30 derniers mouvements (le front en garde 15 affichés)
    rows_m = query("""SELECT mv.id, mv.date_mvt, mv.type_mvt,
                             COALESCE(mv.quantite_unite, 0) AS q,
                             COALESCE(mv.prix_unitaire,  0) AS pu,
                             COALESCE(mv.doc_ref, '') AS doc_ref,
                             COALESCE(mv.notes,   '') AS notes,
                             a.designation AS art_nom,
                             d.nom         AS dep_nom
                      FROM mouvements_stocks mv
                      JOIN articles a ON a.id = mv.article_id
                      JOIN depots   d ON d.id = mv.depot_id
                      ORDER BY mv.id DESC LIMIT 30""")
    mvts = [{
        'id'      : m['id'],
        'date'    : m['date_mvt'] or '',
        'type'    : m['type_mvt'],
        'qte'     : int(float(m['q'])),
        'pu'      : int(float(m['pu'])),
        'valeur'  : int(float(m['q']) * float(m['pu'])),
        'doc_ref' : m['doc_ref'],
        'notes'   : m['notes'],
        'art_nom' : m['art_nom'] or '—',
        'dep_nom' : m['dep_nom'] or '—',
    } for m in rows_m]

    from datetime import datetime
    return jsonify({
        'cursor': cursor,
        'ts'    : datetime.now().strftime('%H:%M:%S'),
        'stocks': stocks,
        'mvts'  : mvts,
        'kpis'  : {
            'valeur_totale' : valeur_totale,
            'nb_ruptures'   : nb_ruptures,
            'nb_alertes'    : nb_alertes,
            'nb_references' : len(stocks),
        }
    })


@app.route('/stock')
@login_required
def stock_list():
    cfg = get_cfg()
    depot_id = request.args.get('depot', '')
    q = request.args.get('q', '')
    depots_all = query("SELECT * FROM depots WHERE actif=1 ORDER BY code")

    sql = """
        SELECT s.*, a.reference, a.designation, a.colisage, a.unite_vente, a.contenance,
               a.prix_vente_ht, a.prix_achat_ht, a.prix_unitaire,
               f.nom as famille_nom, f.couleur as famille_couleur,
               d.nom as depot_nom, d.code as depot_code
        FROM stocks s
        JOIN articles a ON a.id=s.article_id
        JOIN depots d ON d.id=s.depot_id
        LEFT JOIN familles f ON f.id=a.famille_id
        WHERE a.actif=1 AND d.actif=1
    """
    args = []
    if depot_id:
        sql += " AND s.depot_id=?"
        args.append(depot_id)
    if q:
        sql += " AND (a.designation LIKE ? OR a.reference LIKE ?)"
        args += [f'%{q}%', f'%{q}%']
    sql += " ORDER BY a.designation"
    stocks = [dict(r) for r in query(sql, args)]

    # Calcul du PU vente effectif et de la valeur stock
    # Règle valorisation :
    #   valeur_colis  = nb_colis_entiers × prix_vente_ht  (si prix_vente_ht > 0)
    #   valeur_unite  = qte_unites_restantes × prix_unitaire  (si prix_unitaire > 0)
    #   valeur_stock  = valeur_colis + valeur_unite
    for s in stocks:
        pv_ht     = float(s['prix_vente_ht']  or 0)   # prix d'un COLIS
        pu        = float(s['prix_unitaire']   or 0)   # prix d'une UNITÉ (récupéré depuis articles)
        col       = max(int(s['colisage'] or 1), 1)
        qte_u     = float(s['quantite_unite']  or 0)
        nb_col    = int(qte_u // col)                  # colis entiers
        reste_u   = qte_u - nb_col * col               # unités hors colis complets
        s['nb_colis']     = nb_col
        s['reste_unite']  = int(reste_u)
        s['pv_effectif']  = pv_ht if pv_ht > 0 else pu
        # Valorisation séparée : colis + unités restantes
        val_col   = round(nb_col * pv_ht)  if pv_ht > 0 else 0
        val_unite = round(qte_u  * pu)     if pu     > 0 and pv_ht == 0 else round(reste_u * pu)
        s['valeur_colis']  = val_col
        s['valeur_unite']  = val_unite
        s['valeur_stock']  = val_col + val_unite
        # Marge colis = prix_vente_ht − prix_achat_ht (bénéfice sur 1 colis)
        pa_colis = float(s['prix_achat_ht'] or 0)
        if pv_ht > 0:
            s['marge_colis'] = round(pv_ht - pa_colis)
        elif pu > 0:
            s['marge_colis'] = round(pu * col - pa_colis)
        else:
            s['marge_colis'] = 0
        # Marge unité total = (prix_unitaire − prix_achat_ht/colisage) × quantite_unite
        if pu > 0 and col > 0:
            s['marge_unite'] = round((pu - pa_colis / col) * qte_u)
        else:
            s['marge_unite'] = 0

    alertes = [s for s in stocks if s['quantite_unite'] <= s['stock_min_unite'] and s['stock_min_unite'] > 0]
    valeur = sum(s['valeur_stock'] for s in stocks)

    mvts = query("""
        SELECT mv.*, a.designation, d.nom as depot_nom,
               COALESCE(a.colisage, 1)       AS colisage,
               COALESCE(a.prix_vente_ht, 0)  AS prix_vente_ht,
               COALESCE(a.prix_unitaire, 0)  AS prix_unitaire
        FROM mouvements_stocks mv
        JOIN articles a ON a.id=mv.article_id
        JOIN depots d ON d.id=mv.depot_id
        ORDER BY mv.id DESC LIMIT 15
    """)
    # Calcul valeur mouvement : même règle que le stock
    mvts = [dict(m) for m in mvts]
    for m in mvts:
        pv_ht  = float(m['prix_vente_ht'] or 0)
        pu     = float(m['prix_unitaire']  or 0)
        col    = int(m['colisage'] or 1)
        qte_u  = float(m['quantite_unite'] or 0)
        nb_col = qte_u / col if col > 0 else 0
        m['valeur_mvt'] = int(nb_col * pv_ht) if pv_ht > 0 else int(qte_u * pu)
    articles_all = query("SELECT id, designation, colisage, prix_vente_ht, prix_unitaire FROM articles WHERE actif=1 ORDER BY designation")
    return render_template('stock.html', cfg=cfg, stocks=stocks, alertes=alertes,
                           valeur=valeur, mvts=mvts, depots_all=depots_all,
                           articles_all=articles_all, depot_id=depot_id, q=q,
                           now=date.today())

# ══════════════════════════════════════════════════════════════════════
#  INVENTAIRE PHYSIQUE (module séparé)
# ══════════════════════════════════════════════════════════════════════
@app.route('/inventaire')
@login_required
def inventaire_list():
    cfg = get_cfg()
    depot_id = request.args.get('depot', '')
    q = request.args.get('q', '')
    depots_all = query("SELECT * FROM depots WHERE actif=1 ORDER BY code")

    sql = """
        SELECT s.*, a.reference, a.designation, a.colisage, a.unite_vente,
               a.prix_vente_ht, a.prix_achat_ht,
               d.nom as depot_nom, d.code as depot_code
        FROM stocks s
        JOIN articles a ON a.id=s.article_id
        JOIN depots d ON d.id=s.depot_id
        WHERE a.actif=1 AND d.actif=1
    """
    args = []
    if depot_id:
        sql += " AND s.depot_id=?"
        args.append(depot_id)
    if q:
        sql += " AND (a.designation LIKE ? OR a.reference LIKE ?)"
        args += [f'%{q}%', f'%{q}%']
    sql += " ORDER BY a.designation"
    stocks = query(sql, args)

    return render_template('inventaire.html', cfg=cfg, stocks=stocks,
                           depots_all=depots_all, depot_id=depot_id, q=q)

@app.route('/stock/mouvement', methods=['POST'])
@login_required
def stock_mouvement():
    f = request.form
    art_id   = int(f['article_id'])
    depot_id = int(f['depot_id'])
    type_mvt = f['type_mvt']

    # Le JS synchronise les deux champs : quantite_unite est TOUJOURS le total
    # (qu'on saisisse en unités ou en colis). quantite_colis est stocké pour info.
    qte_u = float(f.get('quantite_unite', 0) or 0)
    qte_c = float(f.get('quantite_colis', 0) or 0)

    # Sécurité : si qte_u est 0 mais qte_c > 0 (JS désactivé côté client),
    # on reconvertit manuellement via le colisage de l'article.
    if qte_u == 0 and qte_c > 0:
        art = query("SELECT colisage FROM articles WHERE id=?", (art_id,), one=True)
        if art:
            qte_u = qte_c * (art['colisage'] or 1)

    if qte_u <= 0:
        flash("Quantité nulle — mouvement non enregistré.", "warning")
        return redirect(url_for('stock_list'))

    # Recalculer qte_c cohérent si nécessaire
    if qte_c == 0 and qte_u > 0:
        art = query("SELECT colisage FROM articles WHERE id=?", (art_id,), one=True)
        if art and art['colisage']:
            qte_c = qte_u / art['colisage']

    execute("""INSERT INTO mouvements_stocks(article_id,depot_id,type_mvt,quantite_unite,
               quantite_colis,prix_unitaire,notes,date_mvt,operateur)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (art_id, depot_id, type_mvt, qte_u, qte_c,
             float(f.get('prix_unitaire', 0) or 0),
             f.get('notes'), f.get('date_mvt', date.today().isoformat()),
             session.get('user_nom')))

    # MAJ stock (unités uniquement — source unique de vérité)
    signe = 1 if type_mvt == 'entree' else -1
    execute("""INSERT INTO stocks(article_id,depot_id,quantite_unite)
               VALUES(?,?,?) ON CONFLICT(article_id,depot_id)
               DO UPDATE SET quantite_unite=quantite_unite+?""",
            (art_id, depot_id, signe * qte_u, signe * qte_u))

    # Récupérer le stock actuel et mettre à jour stock_apres sur le dernier mvt
    stock_now = query("SELECT quantite_unite FROM stocks WHERE article_id=? AND depot_id=?",
                      (art_id, depot_id), one=True)
    stock_apres = stock_now['quantite_unite'] if stock_now else 0
    execute("UPDATE mouvements_stocks SET stock_apres=? WHERE id=(SELECT MAX(id) FROM mouvements_stocks WHERE article_id=? AND depot_id=?)",
            (stock_apres, art_id, depot_id))

    art_info = query("SELECT colisage FROM articles WHERE id=?", (art_id,), one=True)
    col = art_info['colisage'] if art_info else 1
    nb_colis = int(qte_u / col) if col else 0
    flash(
        f"Mouvement enregistré : {'+' if signe > 0 else ''}{int(qte_u)} unité(s)"
        f" ({nb_colis} colis) — Stock restant : {int(stock_apres)} unités.",
        "success"
    )
    return redirect(url_for('stock_list'))

@app.route('/stock/seuil/<int:art_id>/<int:dep_id>', methods=['POST'])
@login_required
def stock_seuil(art_id, dep_id):
    seuil = int(request.form.get('stock_min', 0) or 0)
    execute("UPDATE stocks SET stock_min_unite=? WHERE article_id=? AND depot_id=?",
            (seuil, art_id, dep_id))
    flash("Seuil d'alerte mis à jour.", "success")
    return redirect(url_for('stock_list'))

@app.route('/stock/modifier-qte', methods=['POST'])
@login_required
def stock_modifier_qte():
    """Modification DIRECTE de la quantité en stock d'un article dans un dépôt.

    À la différence d'un mouvement entrée/sortie (qui ajoute ou retranche),
    on fixe ici la NOUVELLE quantité totale en unités. L'écart entre l'ancienne
    et la nouvelle valeur est tracé comme mouvement d'ajustement, afin que
    l'historique et la valorisation restent cohérents."""
    f = request.form
    try:
        art_id   = int(f['article_id'])
        depot_id = int(f['depot_id'])
        nouvelle = float(f.get('nouvelle_qte', '') or 0)
    except (KeyError, ValueError, TypeError):
        flash("Données invalides — modification annulée.", "danger")
        return redirect(url_for('stock_list'))

    if nouvelle < 0:
        flash("La quantité ne peut pas être négative.", "warning")
        return redirect(url_for('stock_list'))

    # Quantité actuelle (avant modification)
    row = query("SELECT quantite_unite FROM stocks WHERE article_id=? AND depot_id=?",
                (art_id, depot_id), one=True)
    ancienne = float(row['quantite_unite']) if row else 0.0
    diff = nouvelle - ancienne

    if diff == 0:
        flash("Quantité inchangée.", "info")
        return redirect(url_for('stock_list'))

    # Fixer la nouvelle quantité (les unités sont la source unique de vérité)
    execute("INSERT OR IGNORE INTO stocks(article_id,depot_id,quantite_unite) VALUES(?,?,0)",
            (art_id, depot_id))
    execute("UPDATE stocks SET quantite_unite=? WHERE article_id=? AND depot_id=?",
            (nouvelle, art_id, depot_id))

    # Tracer l'écart comme mouvement d'ajustement
    art = query("SELECT colisage FROM articles WHERE id=?", (art_id,), one=True)
    col = (art['colisage'] if art and art['colisage'] else 1) or 1
    type_mvt = 'entree' if diff > 0 else 'sortie'
    qte_c    = abs(diff) / col if col else 0
    motif    = (f.get('motif') or '').strip()
    notes    = (f"Correction qté : {int(ancienne)} \u2192 {int(nouvelle)} "
                f"({'+' if diff > 0 else ''}{int(diff)} u.)")
    if motif:
        notes += f" \u2014 {motif}"

    execute("""INSERT INTO mouvements_stocks(article_id,depot_id,type_mvt,quantite_unite,
               quantite_colis,doc_type,stock_apres,notes,date_mvt,operateur)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (art_id, depot_id, type_mvt, abs(diff), qte_c, 'ajustement',
             nouvelle, notes, date.today().isoformat(), session.get('user_nom')))

    flash(f"Quantité modifiée : {int(ancienne)} \u2192 {int(nouvelle)} unités "
          f"({'+' if diff > 0 else ''}{int(diff)}).", "success")
    return redirect(url_for('stock_list'))

@app.route('/stock/reinitialiser', methods=['POST'])
@login_required
def stock_reinitialiser():
    """Remet à ZÉRO le stock de tous les articles actuellement affichés
    (mêmes filtres dépôt / recherche que la liste). Remise à zéro brute :
    aucun mouvement n'est enregistré dans l'historique.

    Action destructive : réservée aux administrateurs."""
    if session.get('user_role') != 'admin':
        flash("Action réservée aux administrateurs.", "danger")
        return redirect(url_for('stock_list'))
    f = request.form
    depot_id = (f.get('depot') or '').strip()
    q        = (f.get('q') or '').strip()

    # Reproduit exactement le filtrage de stock_list
    base = (" FROM stocks s "
            "JOIN articles a ON a.id=s.article_id "
            "JOIN depots d ON d.id=s.depot_id "
            "WHERE a.actif=1 AND d.actif=1")
    args = []
    if depot_id:
        base += " AND s.depot_id=?"
        args.append(depot_id)
    if q:
        base += " AND (a.designation LIKE ? OR a.reference LIKE ?)"
        args += [f'%{q}%', f'%{q}%']

    row = query("SELECT COUNT(*) AS n" + base, args, one=True)
    n = row['n'] if row else 0
    if not n:
        flash("Aucun stock à réinitialiser.", "info")
        return redirect(url_for('stock_list', depot=depot_id, q=q))

    execute("UPDATE stocks SET quantite_unite=0, quantite_colis=0 "
            "WHERE id IN (SELECT s.id" + base + ")", args)

    flash(f"Stock réinitialisé à zéro pour {n} article(s) affiché(s).", "success")
    return redirect(url_for('stock_list', depot=depot_id, q=q))

# ══════════════════════════════════════════════════════════════════════
#  CLIENTS
# ══════════════════════════════════════════════════════════════════════
@app.route('/clients')
@login_required
def clients_list():
    cfg = get_cfg()
    q = request.args.get('q', '')
    sql = """SELECT c.*,
             COUNT(DISTINCT dv.id) as nb_factures,
             COALESCE(SUM(CASE WHEN dv.statut IN ('en_attente','partielle') THEN dv.reste ELSE 0 END),0) as encours
             FROM clients c
             LEFT JOIN documents_vente dv ON dv.client_id=c.id AND dv.type_doc='facture'
             WHERE c.actif=1"""
    args = []
    if q:
        sql += " AND (c.nom LIKE ? OR c.prenom LIKE ? OR c.telephone LIKE ?)"
        args += [f'%{q}%', f'%{q}%', f'%{q}%']
    sql += " GROUP BY c.id ORDER BY CASE WHEN c.code='CLI000' THEN 0 ELSE 1 END, c.nom"
    clients = query(sql, args)
    # Client par défaut CLI000 — transmis aux templates pour pré-sélection
    client_defaut = query("SELECT * FROM clients WHERE code='CLI000' AND actif=1", one=True)
    return render_template('clients.html', cfg=cfg, clients=clients, q=q,
                           client_defaut=client_defaut)

@app.route('/clients/add', methods=['POST'])
@login_required
def client_add():
    f = request.form
    # Générer un code unique en évitant CLI000 (réservé au client passager)
    n = query("SELECT COUNT(*) as c FROM clients", one=True)['c']
    code = f"CLI{n+1:04d}"
    # S'assurer que CLI0001+ ne rentre pas en collision si CLI000 existe déjà
    while query("SELECT id FROM clients WHERE code=?", (code,), one=True):
        n += 1
        code = f"CLI{n+1:04d}"
    execute("""INSERT INTO clients(code,nom,prenom,type_client,telephone,telephone2,
               email,adresse,ville,zone_livraison,encours_autorise,remise_pct,
               mode_paiement,delai_paiement,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, f['nom'].upper(), f.get('prenom'),
             f.get('type_client','particulier'),
             f.get('telephone'), f.get('telephone2'),
             f.get('email'), f.get('adresse'),
             f.get('ville','Abidjan'),
             f.get('zone_livraison'),
             float(f.get('encours_autorise',0) or 0),
             float(f.get('remise_pct',0) or 0),
             f.get('mode_paiement','especes'),
             int(f.get('delai_paiement',0) or 0),
             f.get('notes')))
    flash("Client ajouté.", "success")
    return redirect(url_for('clients_list'))

@app.route('/clients/edit/<int:id>', methods=['POST'])
@login_required
def client_edit(id):
    f = request.form
    execute("""UPDATE clients SET nom=?,prenom=?,type_client=?,telephone=?,telephone2=?,
               email=?,adresse=?,ville=?,zone_livraison=?,encours_autorise=?,remise_pct=?,
               mode_paiement=?,delai_paiement=?,notes=? WHERE id=?""",
            (f['nom'].upper(), f.get('prenom'),
             f.get('type_client','particulier'),
             f.get('telephone'), f.get('telephone2'),
             f.get('email'), f.get('adresse'),
             f.get('ville','Abidjan'),
             f.get('zone_livraison'),
             float(f.get('encours_autorise',0) or 0),
             float(f.get('remise_pct',0) or 0),
             f.get('mode_paiement','especes'),
             int(f.get('delai_paiement',0) or 0),
             f.get('notes'), id))
    flash("Client modifié.", "success")
    return redirect(url_for('clients_list'))

@app.route('/clients/delete/<int:id>')
@login_required
def client_delete(id):
    c = query("SELECT code FROM clients WHERE id=?", (id,), one=True)
    if c and c['code'] == 'CLI000':
        flash("Le client par défaut (CLI000) ne peut pas être supprimé.", "warning")
        return redirect(url_for('clients_list'))
    execute("UPDATE clients SET actif=0 WHERE id=?", (id,))
    flash("Client supprimé.", "success")
    return redirect(url_for('clients_list'))

@app.route('/api/clients')
@login_required
def api_clients():
    q = request.args.get('q', '')
    rows = query("""SELECT id, nom, prenom, telephone, remise_pct, mode_paiement, encours_autorise
                    FROM clients WHERE actif=1 AND (nom LIKE ? OR prenom LIKE ? OR telephone LIKE ?)
                    ORDER BY nom LIMIT 20""",
                 (f'%{q}%', f'%{q}%', f'%{q}%'))
    return jsonify([dict(r) for r in rows])

@app.route('/api/client/add', methods=['POST'])
@login_required
def api_client_add():
    """Création rapide de client depuis une modale, renvoie JSON avec l'id et le label."""
    f = request.form
    if not f.get('nom'):
        return jsonify({'error': 'Le nom est obligatoire.'}), 400
    n = query("SELECT COUNT(*) as c FROM clients", one=True)['c']
    code = f"CLI{n+1:04d}"
    while query("SELECT id FROM clients WHERE code=?", (code,), one=True):
        n += 1
        code = f"CLI{n+1:04d}"
    execute("""INSERT INTO clients(code,nom,prenom,type_client,telephone,telephone2,
               email,adresse,ville,zone_livraison,encours_autorise,remise_pct,
               mode_paiement,delai_paiement,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, f['nom'].upper(), f.get('prenom'),
             f.get('type_client', 'particulier'),
             f.get('telephone'), f.get('telephone2'),
             f.get('email'), f.get('adresse'),
             f.get('ville', 'Abidjan'),
             f.get('zone_livraison'),
             float(f.get('encours_autorise', 0) or 0),
             float(f.get('remise_pct', 0) or 0),
             f.get('mode_paiement', 'especes'),
             int(f.get('delai_paiement', 0) or 0),
             f.get('notes')))
    row = query("SELECT id, nom, prenom, telephone FROM clients WHERE code=?", (code,), one=True)
    prenom = row['prenom'] or ''
    label  = (row['nom'] + (' ' + prenom if prenom else '')).strip()
    if row['telephone']:
        label += ' (' + row['telephone'] + ')'
    return jsonify({'id': row['id'], 'label': label, 'nom': row['nom'], 'prenom': prenom})

# ══════════════════════════════════════════════════════════════════════
#  FOURNISSEURS
# ══════════════════════════════════════════════════════════════════════
@app.route('/fournisseurs')
@login_required
def fournisseurs_list():
    cfg = get_cfg()
    q = request.args.get('q', '')
    sql = """SELECT f.*, COUNT(DISTINCT da.id) as nb_commandes,
             COALESCE(SUM(CASE WHEN da.statut IN ('en_attente','partielle') THEN da.reste ELSE 0 END),0) as encours
             FROM fournisseurs f
             LEFT JOIN documents_achat da ON da.fournisseur_id=f.id
             WHERE f.actif=1"""
    args = []
    if q:
        sql += " AND (f.nom LIKE ? OR f.contact LIKE ? OR f.telephone LIKE ?)"
        args += [f'%{q}%', f'%{q}%', f'%{q}%']
    sql += " GROUP BY f.id ORDER BY f.nom"
    fournisseurs = query(sql, args)
    return render_template('fournisseurs.html', cfg=cfg, fournisseurs=fournisseurs, q=q)

@app.route('/fournisseurs/add', methods=['POST'])
@login_required
def fournisseur_add():
    f = request.form
    n = query("SELECT COUNT(*) as c FROM fournisseurs", one=True)['c']
    code = f"FOU{n+1:03d}"
    execute("""INSERT INTO fournisseurs(code,nom,contact,telephone,email,adresse,ville,pays,
               type_produits,delai_livraison,conditions_paiement,remise_pct,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, f['nom'].upper(), f.get('contact'),
             f.get('telephone'), f.get('email'),
             f.get('adresse'), f.get('ville','Abidjan'),
             f.get('pays',"Côte d'Ivoire"),
             f.get('type_produits'), f.get('delai_livraison'),
             f.get('conditions_paiement'),
             float(f.get('remise_pct',0) or 0),
             f.get('notes')))
    flash("Fournisseur ajouté.", "success")
    return redirect(url_for('fournisseurs_list'))

@app.route('/fournisseurs/edit/<int:id>', methods=['POST'])
@login_required
def fournisseur_edit(id):
    f = request.form
    execute("""UPDATE fournisseurs SET nom=?,contact=?,telephone=?,email=?,adresse=?,ville=?,
               pays=?,type_produits=?,delai_livraison=?,conditions_paiement=?,remise_pct=?,notes=?
               WHERE id=?""",
            (f['nom'].upper(), f.get('contact'), f.get('telephone'), f.get('email'),
             f.get('adresse'), f.get('ville','Abidjan'),
             f.get('pays',"Côte d'Ivoire"),
             f.get('type_produits'), f.get('delai_livraison'),
             f.get('conditions_paiement'),
             float(f.get('remise_pct',0) or 0),
             f.get('notes'), id))
    flash("Fournisseur modifié.", "success")
    return redirect(url_for('fournisseurs_list'))

@app.route('/fournisseurs/delete/<int:id>')
@login_required
def fournisseur_delete(id):
    execute("UPDATE fournisseurs SET actif=0 WHERE id=?", (id,))
    flash("Fournisseur supprimé.", "success")
    return redirect(url_for('fournisseurs_list'))

# ══════════════════════════════════════════════════════════════════════
#  DOCUMENTS VENTE (Devis / Commandes / Factures)
# ══════════════════════════════════════════════════════════════════════
def _get_doc_vente(type_doc, q='', statut_f=''):
    sql = """SELECT dv.*, COALESCE(c.nom, 'CLIENT PASSAGER') as client_nom, c.telephone as client_tel
             FROM documents_vente dv LEFT JOIN clients c ON c.id=dv.client_id
             WHERE dv.type_doc=?"""
    args = [type_doc]
    if q:
        sql += " AND (dv.reference LIKE ? OR c.nom LIKE ?)"
        args += [f'%{q}%', f'%{q}%']
    if statut_f:
        sql += " AND dv.statut=?"
        args.append(statut_f)
    sql += " ORDER BY dv.date_creation DESC"
    return query(sql, args)

@app.route('/devis')
@login_required
def devis_list():
    cfg = get_cfg()
    q = request.args.get('q','')
    statut_f = request.args.get('statut','')
    docs = _get_doc_vente('devis', q, statut_f)
    clients_all = query("SELECT id, nom, prenom, telephone FROM clients WHERE actif=1 ORDER BY nom")
    # Enrichir avec un label affichable dans le <select> du formulaire
    clients_all = [dict(c) for c in clients_all]
    for c in clients_all:
        c['label'] = (c['nom'] or '') + (' ' + c['prenom'] if c.get('prenom') else '') + (' — ' + c['telephone'] if c.get('telephone') else '')
    depots_all  = query("SELECT id, code, nom FROM depots WHERE actif=1")
    tva_def = float(cfg.get('tva_collectee_taux', cfg.get('tva', 0)) or 0)
    # KPIs
    kpi = query("""SELECT
        COALESCE(SUM(total_ttc),0)                                        as ca_total,
        COALESCE(SUM(CASE WHEN strftime('%Y-%m',date_doc)=strftime('%Y-%m','now') THEN total_ttc ELSE 0 END),0) as ca_mois,
        COUNT(*)                                                           as nb_total,
        COUNT(CASE WHEN statut NOT IN ('converti','annule') THEN 1 END)   as nb_attente
        FROM documents_vente WHERE type_doc='devis'""", one=True)
    articles_all = query("""SELECT a.id, a.reference, a.designation, a.prix_vente_ht,
               a.tva, a.colisage, a.unite_vente,
               COALESCE(SUM(s.quantite_unite),0) as stock_total
               FROM articles a
               LEFT JOIN stocks s ON s.article_id=a.id
               WHERE a.actif=1
               GROUP BY a.id ORDER BY a.designation""")
    client_passager = query("SELECT id FROM clients WHERE code='CLI000' AND actif=1", one=True)
    passager_id = client_passager['id'] if client_passager else None
    return render_template('devis.html', cfg=cfg, docs=docs, q=q, statut_f=statut_f,
                           clients_all=clients_all, depots_all=depots_all,
                           articles_all=articles_all,
                           type_doc='devis', tva_collectee=tva_def, today=date.today().isoformat(),
                           ca_total=kpi['ca_total'], ca_mois=kpi['ca_mois'],
                           nb_total=kpi['nb_total'], nb_attente=kpi['nb_attente'],
                           passager_id=passager_id)

@app.route('/devis/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def devis_edit(id):
    """Modifier un devis (uniquement si non converti / non annulé)."""
    dv = query("SELECT * FROM documents_vente WHERE id=? AND type_doc='devis'", (id,), one=True)
    if not dv:
        flash("Devis introuvable.", "danger")
        return redirect(url_for('devis_list'))
    if dv['statut'] in ('converti', 'annule'):
        flash("Ce devis a déjà été converti ou annulé et ne peut plus être modifié.", "warning")
        return redirect(url_for('devis_list'))

    if request.method == 'POST':
        f = request.form
        lignes = json.loads(f.get('lignes_json', '[]'))
        tva_def = float(get_cfg().get('tva_collectee_taux', get_cfg().get('tva', 0)) or 0)
        total_ht = total_tva = total_ttc = 0
        for l in lignes:
            _col   = max(1, int(l.get('colisage', 1) or 1))
            _qte_u = float(l.get('qte_unite', l.get('quantite', 0)) or 0)
            _qte_c = float(l.get('qte_colis', l.get('colis', 0)) or 0)
            if _qte_c > 0 and _qte_u == 0:
                _qte_u = _qte_c * _col
            elif _qte_u > 0 and _qte_c == 0 and _col > 1:
                _qte_c = int(_qte_u // _col)
            _mode  = l.get('mode_saisie', 'unite')
            _rem   = float(l.get('remise', l.get('remise_pct', 0)) or 0)
            # Priorite : montants deja calcules par le JS (ht_ligne / ttc_ligne).
            # Evite le decalage prix_colis vs prix_unitaire lors d'une edition.
            _ht_js  = float(l.get('ht_ligne',  l.get('total_ht',  0)) or 0)
            _ttc_js = float(l.get('ttc_ligne', l.get('total_ttc', 0)) or 0)
            if _ht_js > 0:
                _ht   = round(_ht_js)
                _ttc  = round(_ttc_js) if _ttc_js > 0 else _ht
                _tvam = _ttc - _ht
            else:
                _base = _qte_c if _mode == 'colis' else _qte_u
                _prix = float(l.get('prix_ht', 0))
                _ht   = round(_base * _prix * (1 - _rem / 100))
                _tvam = round(_ht * tva_def / 100)
                _ttc  = _ht + _tvam
            l['_qte_u'] = _qte_u; l['_qte_c'] = _qte_c
            l['_ht'] = _ht; l['_ttc'] = _ttc
            total_ht  += _ht
            total_tva += _tvam
            total_ttc += _ttc

        remise = float(f.get('remise_globale', 0) or 0)
        if remise:
            total_ht  *= (1 - remise / 100)
            total_tva *= (1 - remise / 100)
            total_ttc *= (1 - remise / 100)

        execute("""UPDATE documents_vente SET
                   client_id=?, depot_id=?, date_doc=?, date_echeance=?,
                   remise_globale=?, total_ht=?, total_tva=?, total_ttc=?,
                   reste=?, mode_paiement=?, notes=?
                   WHERE id=?""",
                (_safe_fk(f.get('client_id')),
                 resolve_depot_id(f.get('depot_id')),
                 f.get('date_doc', date.today().isoformat()),
                 f.get('date_echeance') or None,
                 remise, round(total_ht, 2), round(total_tva, 2), round(total_ttc, 2),
                 round(total_ttc, 2),
                 f.get('mode_paiement', 'especes'),
                 f.get('notes', ''),
                 id))

        # Remplacer les lignes
        execute("DELETE FROM lignes_vente WHERE document_id=?", (id,))
        for i, l in enumerate(lignes):
            execute("""INSERT INTO lignes_vente(document_id,article_id,designation,
                       quantite_unite,quantite_colis,prix_ht,remise_pct,tva,total_ht,total_ttc,num_ligne)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (id, l.get('article_id') or l.get('stock_id') or None, l.get('designation', ''),
                     l['_qte_u'], l['_qte_c'],
                     float(l.get('prix_ht', 0)),
                     float(l.get('remise', l.get('remise_pct', 0)) or 0),
                     tva_def, l['_ht'], l['_ttc'], i + 1))

        flash(f"Devis {dv['reference']} mis à jour.", "success")
        return redirect(url_for('devis_list'))

    # GET : préparer les données pour le formulaire d'édition
    cfg = get_cfg()
    lignes_db = query("""SELECT lv.*, a.reference as art_ref, a.colisage, a.unite_vente
                         FROM lignes_vente lv
                         LEFT JOIN articles a ON a.id=lv.article_id
                         WHERE lv.document_id=? ORDER BY lv.num_ligne""", (id,))
    clients_all  = query("SELECT id, nom, prenom, telephone FROM clients WHERE actif=1 ORDER BY nom")
    depots_all   = query("SELECT id, code, nom FROM depots WHERE actif=1")
    articles_all = query("""SELECT a.id, a.reference, a.designation, a.prix_vente_ht,
                            a.tva, a.colisage, a.unite_vente,
                            COALESCE(SUM(s.quantite_unite),0) as stock_total
                            FROM articles a
                            LEFT JOIN stocks s ON s.article_id=a.id
                            WHERE a.actif=1
                            GROUP BY a.id ORDER BY a.designation""")
    tva_def = float(cfg.get('tva_collectee_taux', cfg.get('tva', 0)) or 0)
    edit_doc_d    = dict(dv)
    edit_lignes_d = [dict(l) for l in lignes_db]
    clients_list  = [dict(c) for c in clients_all]
    for c in clients_list:
        c['label'] = (c['nom'] or '') + (' ' + c['prenom'] if c.get('prenom') else '') + (' — ' + c['telephone'] if c.get('telephone') else '')
    kpi = query("""SELECT COUNT(*) as nb_total,
                   COALESCE(SUM(total_ttc),0) as ca_total,
                   COALESCE(SUM(CASE WHEN strftime('%Y-%m',date_doc)=strftime('%Y-%m','now') THEN total_ttc ELSE 0 END),0) as ca_mois,
                   COUNT(CASE WHEN statut NOT IN ('converti','annule') THEN 1 END) as nb_attente
                   FROM documents_vente WHERE type_doc='devis'""", one=True)
    client_passager = query("SELECT id FROM clients WHERE code='CLI000' AND actif=1", one=True)
    passager_id = client_passager['id'] if client_passager else None
    return render_template('devis.html', cfg=cfg,
                           docs=[], q='', statut_f='',
                           clients_all=clients_list, depots_all=depots_all,
                           articles_all=articles_all,
                           tva_collectee=tva_def,
                           today=date.today().isoformat(),
                           ca_total=kpi['ca_total'], ca_mois=kpi['ca_mois'],
                           nb_total=kpi['nb_total'], nb_attente=kpi['nb_attente'],
                           passager_id=passager_id,
                           edit_doc=edit_doc_d, edit_lignes=edit_lignes_d)


@app.route('/commandes')
@login_required
def commandes_vente_list():
    cfg = get_cfg()
    q = request.args.get('q','')
    statut_f = request.args.get('statut','')
    docs = _get_doc_vente('commande', q, statut_f)
    clients_all = query("SELECT id, nom, prenom, telephone FROM clients WHERE actif=1 ORDER BY nom")
    # Enrichir avec un label affichable dans le <select> du formulaire
    clients_all = [dict(c) for c in clients_all]
    for c in clients_all:
        c['label'] = (c['nom'] or '') + (' ' + c['prenom'] if c.get('prenom') else '') + (' — ' + c['telephone'] if c.get('telephone') else '')
    depots_all  = query("SELECT id, code, nom FROM depots WHERE actif=1")
    tva_def = float(cfg.get('tva_collectee_taux', cfg.get('tva', 0)) or 0)
    # KPIs
    kpi = query("""SELECT
        COALESCE(SUM(total_ttc),0)                                           as ca_total,
        COALESCE(SUM(CASE WHEN strftime('%Y-%m',date_doc)=strftime('%Y-%m','now') THEN total_ttc ELSE 0 END),0) as ca_mois,
        COUNT(*)                                                             as nb_total,
        COUNT(CASE WHEN statut NOT IN ('facturee','annulee','bl_cree') THEN 1 END) as nb_attente
        FROM documents_vente WHERE type_doc='commande'""", one=True)
    articles_all = query("""SELECT a.id, a.reference, a.designation, a.prix_vente_ht,
               a.tva, a.colisage, a.unite_vente,
               COALESCE(SUM(s.quantite_unite),0) as stock_total
               FROM articles a
               LEFT JOIN stocks s ON s.article_id=a.id
               WHERE a.actif=1
               GROUP BY a.id ORDER BY a.designation""")
    # Règlements libres disponibles pour liaison commande
    reglements_libres = query("""SELECT r.*, c.nom as client_nom
                                  FROM reglements r
                                  LEFT JOIN clients c ON c.id=r.client_id
                                  WHERE r.source_type IN ('libre','commande')
                                  ORDER BY r.date_reglement DESC""")
    return render_template('commandes_vente.html', cfg=cfg, docs=docs, q=q, statut_f=statut_f,
                           clients_all=clients_all, depots_all=depots_all,
                           articles_all=articles_all,
                           type_doc='commande', tva_collectee=tva_def, today=date.today().isoformat(),
                           ca_total=kpi['ca_total'], ca_mois=kpi['ca_mois'],
                           nb_total=kpi['nb_total'], nb_attente=kpi['nb_attente'],
                           reglements_libres=reglements_libres)


# ══════════════════════════════════════════════════════════════════════
#  INTÉGRATION FNE — Facture Normalisée Électronique (DGI Côte d'Ivoire)
#  Documentation officielle : https://www.fne.dgi.gouv.ci/documents/FNE-procedureapi.pdf
# ══════════════════════════════════════════════════════════════════════

def _fne_get_config():
    """Retourne (url, api_key, etablissement, point_vente, vendeur) ou None si pas configuré."""
    cfg = get_cfg()
    api_key = (cfg.get('cle_api_fne') or '').strip()
    url     = (cfg.get('fne_url') or 'http://54.247.95.108/ws').strip().rstrip('/')
    if not api_key:
        return None
    return {
        'url':           url,
        'api_key':       api_key,
        'etablissement': (cfg.get('fne_etablissement') or cfg.get('nom_depot') or '').strip(),
        'point_vente':   (cfg.get('fne_point_vente') or '01').strip(),
        'vendeur':       (cfg.get('fne_vendeur') or '').strip(),
        'mode':          (cfg.get('fne_mode') or 'test').strip(),
    }


def _fne_tva_code(taux):
    """Convertit un taux TVA (%) en code FNE (TVA / TVAB / TVAC / TVAD)."""
    try:
        t = float(taux or 0)
    except (TypeError, ValueError):
        t = 0
    if t >= 17:   return 'TVA'    # 18% — TVA normal
    if t >= 8:    return 'TVAB'   # 9%  — TVA réduit
    return 'TVAC'                 # 0%  — Exonération conventionnelle (cas le plus courant)


def _fne_payment_method(mode_app):
    """Convertit un mode de paiement interne DISTRIGEST vers le code FNE."""
    mapping = {
        'especes':        'cash',
        'wave':           'mobile-money',
        'orange_money':   'mobile-money',
        'mtn_money':      'mobile-money',
        'moov_money':     'mobile-money',
        'carte_bancaire': 'card',
        'cheque':         'check',
        'virement':       'transfer',
    }
    return mapping.get((mode_app or '').lower().strip(), 'cash')


def _fne_template(client):
    """Détermine le template FNE selon le profil client : B2B / B2C / B2G / B2F."""
    if not client:
        return 'B2C'
    # NCC renseigné (matricule fiscal) → entreprise/professionnel
    if (client.get('matricule_fiscal') or '').strip():
        return 'B2B'
    # Secteur "gouvernement" / "administration" → B2G
    sect = (client.get('secteur') or '').lower()
    if any(k in sect for k in ('gouv', 'public', 'administration', 'etat', 'ministère', 'ministere')):
        return 'B2G'
    # Catégorie "international" / "etranger" → B2F
    cat = (client.get('categorie_client') or '').lower()
    if 'international' in cat or 'etranger' in cat or 'étranger' in cat:
        return 'B2F'
    return 'B2C'


def transmettre_fne_vente(facture_id):
    """
    Transmet une facture de vente à la plateforme FNE et certifie son n° fiscal.
    Met à jour documents_vente avec le statut + référence DGI + QR token.
    Retourne (success: bool, message: str, payload: dict|None).
    """
    import urllib.request, urllib.error

    conf = _fne_get_config()
    if not conf:
        return False, "Clé API FNE non configurée. Renseignez-la dans Paramètres → Facture normalisée.", None

    # ── Lire la facture ──
    fac = query("SELECT * FROM documents_vente WHERE id=? AND type_doc='facture'",
                (facture_id,), one=True)
    if not fac:
        return False, "Facture introuvable.", None

    # Déjà certifiée — éviter un double envoi
    if (fac['fne_statut'] or '') == 'certifiee':
        return False, f"Cette facture est déjà certifiée (référence DGI : {fac['fne_reference']}).", None

    # ── Lire client ──
    client = None
    if fac['client_id']:
        client = query("SELECT * FROM clients WHERE id=?", (fac['client_id'],), one=True)

    # ── Lire lignes ──
    lignes = query("""SELECT lv.*, a.reference as art_ref, a.unite_vente
                      FROM lignes_vente lv
                      LEFT JOIN articles a ON a.id = lv.article_id
                      WHERE lv.document_id=? ORDER BY lv.num_ligne, lv.id""",
                   (facture_id,))
    if not lignes:
        return False, "Cette facture ne contient aucune ligne.", None

    # ── Construire les items au format FNE ──
    items = []
    for l in lignes:
        qte = float(l['quantite_unite'] or 0)
        if qte <= 0:
            qte = float(l['quantite_colis'] or 0)
        items.append({
            "taxes":           [_fne_tva_code(l['tva'])],
            "reference":       (l['art_ref'] or '')[:50],
            "description":     (l['designation'] or 'Article')[:200],
            "quantity":        qte,
            "amount":          round(float(l['prix_ht'] or 0), 2),
            "discount":        round(float(l['remise_pct'] or 0), 2),
            "measurementUnit": (l['unite_vente'] or 'pcs')[:20],
        })

    # ── Payload principal ──
    template = _fne_template(client) if client else 'B2C'
    payload = {
        "invoiceType":       "sale",
        "paymentMethod":     _fne_payment_method(fac['mode_paiement']),
        "template":          template,
        "isRne":             False,
        "clientCompanyName": (client['nom'] if client else 'Client passager')[:200],
        "clientPhone":       (client['telephone'] if client and client.get('telephone') else ''),
        "clientEmail":       (client['email'] if client and client.get('email') else ''),
        "clientSellerName":  conf['vendeur'],
        "pointOfSale":       conf['point_vente'],
        "establishment":     conf['etablissement'] or 'Établissement principal',
        "commercialMessage": '',
        "footer":            '',
        "foreignCurrency":   '',
        "foreignCurrencyRate": 0,
        "items":             items,
        "discount":          round(float(fac['remise_globale'] or 0), 2),
    }
    # NCC obligatoire pour B2B
    if template == 'B2B' and client and client.get('matricule_fiscal'):
        payload['clientNcc'] = client['matricule_fiscal'].strip()

    # ── Appel HTTP ──
    endpoint = conf['url'] + '/external/invoices/sign'
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        endpoint,
        data=body,
        method='POST',
        headers={
            'Content-Type':  'application/json',
            'Accept':        'application/json',
            'Authorization': 'Bearer ' + conf['api_key'],
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode('utf-8')
            data = json.loads(raw) if raw else {}
            # ── Succès ──
            ref_dgi   = data.get('reference') or ''
            qr_token  = data.get('token') or ''
            inv_obj   = data.get('invoice') or {}
            inv_id    = inv_obj.get('id') or ''
            balance   = data.get('balance_sticker')
            execute("""UPDATE documents_vente SET
                       fne_statut='certifiee',
                       fne_reference=?,
                       fne_invoice_id=?,
                       fne_qr_token=?,
                       fne_date_transmission=?,
                       fne_message_erreur=NULL,
                       fne_balance_sticker=?
                       WHERE id=?""",
                    (ref_dgi, inv_id, qr_token,
                     datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                     balance, facture_id))

            # ── Persister les UUIDs DGI par ligne (pour avoirs ultérieurs) ──
            # La DGI retourne les items dans l'ordre où on les a envoyés.
            ret_items = inv_obj.get('items') or []
            for idx, l in enumerate(lignes):
                if idx < len(ret_items):
                    fne_item_id = ret_items[idx].get('id') or ''
                    if fne_item_id:
                        execute("UPDATE lignes_vente SET fne_item_id=? WHERE id=?",
                                (fne_item_id, l['id']))

            return True, f"Facture certifiée DGI : {ref_dgi}", data

    except urllib.error.HTTPError as e:
        # 400 / 401 / 500 — corps d'erreur JSON
        err_raw = ''
        try:
            err_raw = e.read().decode('utf-8')
            err_data = json.loads(err_raw) if err_raw else {}
            msg = err_data.get('message') or err_raw or str(e)
        except Exception:
            msg = err_raw or str(e)
        execute("""UPDATE documents_vente SET
                   fne_statut='erreur',
                   fne_message_erreur=?,
                   fne_date_transmission=?
                   WHERE id=?""",
                (f"HTTP {e.code} — {msg}"[:500],
                 datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                 facture_id))
        return False, f"Erreur DGI ({e.code}) : {msg}", None

    except urllib.error.URLError as e:
        msg = f"Connexion impossible à la plateforme FNE : {e.reason}"
        execute("""UPDATE documents_vente SET
                   fne_statut='erreur',
                   fne_message_erreur=?,
                   fne_date_transmission=?
                   WHERE id=?""",
                (msg[:500], datetime.now().strftime('%Y-%m-%d %H:%M:%S'), facture_id))
        return False, msg, None

    except Exception as e:
        msg = f"Erreur inattendue : {e}"
        execute("""UPDATE documents_vente SET
                   fne_statut='erreur',
                   fne_message_erreur=?,
                   fne_date_transmission=?
                   WHERE id=?""",
                (msg[:500], datetime.now().strftime('%Y-%m-%d %H:%M:%S'), facture_id))
        return False, msg, None


@app.route('/factures/<int:id>/fne', methods=['POST'])
@login_required
def facture_transmettre_fne(id):
    """Déclenche la transmission de la facture <id> à la plateforme FNE."""
    if _get_perm('acces_factures') != 'ecriture':
        flash("Permission refusée.", "danger")
        return redirect(url_for('factures_list'))

    success, message, _data = transmettre_fne_vente(id)
    if success:
        flash(f"📡 {message}", "success")
    else:
        flash(f"❌ FNE : {message}", "danger")
    return redirect(url_for('factures_list'))


def transmettre_fne_avoir(avoir_id):
    """
    Transmet un avoir à la plateforme FNE pour certification.
    L'avoir doit être lié à une facture déjà certifiée (fne_invoice_id non nul).
    Endpoint DGI : POST $url/external/invoices/{invoice_id}/refund
    Retourne (success: bool, message: str, payload: dict|None).
    """
    import urllib.request, urllib.error

    conf = _fne_get_config()
    if not conf:
        return False, "Clé API FNE non configurée.", None

    # ── Lire l'avoir ──
    av = query("SELECT * FROM avoirs_clients WHERE id=?", (avoir_id,), one=True)
    if not av:
        return False, "Avoir introuvable.", None
    if (av['fne_statut'] or '') == 'certifie':
        return False, f"Avoir déjà certifié (réf. DGI : {av['fne_reference']}).", None
    if not av['facture_id']:
        return False, "Avoir non lié à une facture. La DGI exige une facture d'origine certifiée.", None

    # ── Récupérer la facture d'origine ──
    fac = query("SELECT * FROM documents_vente WHERE id=?", (av['facture_id'],), one=True)
    if not fac:
        return False, "Facture d'origine introuvable.", None
    if not fac['fne_invoice_id']:
        return False, "La facture d'origine n'a pas encore été certifiée à la FNE. Certifiez-la d'abord.", None

    # ── Lire les lignes de l'avoir + matcher avec les lignes facture certifiées ──
    lignes_av = query("""SELECT * FROM lignes_avoir_client WHERE avoir_id=?""", (avoir_id,))
    if not lignes_av:
        return False, "Cet avoir ne contient aucune ligne.", None

    lignes_fac = query("""SELECT id, article_id, designation, fne_item_id
                          FROM lignes_vente WHERE document_id=?""", (av['facture_id'],))
    # Index : article_id → fne_item_id, designation_lc → fne_item_id (fallback)
    by_article = {l['article_id']: l['fne_item_id'] for l in lignes_fac
                  if l['article_id'] and l['fne_item_id']}
    by_desig   = {(l['designation'] or '').strip().lower(): l['fne_item_id']
                  for l in lignes_fac if l['fne_item_id']}

    items = []
    for l in lignes_av:
        fne_id = None
        if l['article_id'] and l['article_id'] in by_article:
            fne_id = by_article[l['article_id']]
        else:
            fne_id = by_desig.get((l['designation'] or '').strip().lower())
        if not fne_id:
            return False, (f"Ligne avoir « {l['designation']} » sans correspondance dans la facture FNE. "
                           f"Vérifiez que l'article est bien présent dans la facture d'origine certifiée."), None
        items.append({
            "id":       fne_id,
            "quantity": float(l['quantite'] or 0),
        })

    payload = {"items": items}

    # ── Appel HTTP ──
    endpoint = conf['url'] + '/external/invoices/' + fac['fne_invoice_id'] + '/refund'
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        endpoint, data=body, method='POST',
        headers={
            'Content-Type':  'application/json',
            'Accept':        'application/json',
            'Authorization': 'Bearer ' + conf['api_key'],
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode('utf-8')
            data = json.loads(raw) if raw else {}
            ref_dgi  = data.get('reference') or ''
            qr_token = data.get('token') or ''
            execute("""UPDATE avoirs_clients SET
                       fne_statut='certifie',
                       fne_reference=?,
                       fne_qr_token=?,
                       fne_date_transmission=?,
                       fne_message_erreur=NULL
                       WHERE id=?""",
                    (ref_dgi, qr_token,
                     datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                     avoir_id))
            return True, f"Avoir certifié DGI : {ref_dgi}", data

    except urllib.error.HTTPError as e:
        err_raw = ''
        try:
            err_raw = e.read().decode('utf-8')
            err_data = json.loads(err_raw) if err_raw else {}
            msg = err_data.get('message') or err_raw or str(e)
        except Exception:
            msg = err_raw or str(e)
        execute("""UPDATE avoirs_clients SET
                   fne_statut='erreur', fne_message_erreur=?, fne_date_transmission=?
                   WHERE id=?""",
                (f"HTTP {e.code} — {msg}"[:500],
                 datetime.now().strftime('%Y-%m-%d %H:%M:%S'), avoir_id))
        return False, f"Erreur DGI ({e.code}) : {msg}", None

    except urllib.error.URLError as e:
        msg = f"Connexion impossible à la plateforme FNE : {e.reason}"
        execute("""UPDATE avoirs_clients SET
                   fne_statut='erreur', fne_message_erreur=?, fne_date_transmission=?
                   WHERE id=?""",
                (msg[:500], datetime.now().strftime('%Y-%m-%d %H:%M:%S'), avoir_id))
        return False, msg, None

    except Exception as e:
        msg = f"Erreur inattendue : {e}"
        execute("""UPDATE avoirs_clients SET
                   fne_statut='erreur', fne_message_erreur=?, fne_date_transmission=?
                   WHERE id=?""",
                (msg[:500], datetime.now().strftime('%Y-%m-%d %H:%M:%S'), avoir_id))
        return False, msg, None


@app.route('/avoirs/<int:id>/fne', methods=['POST'])
@login_required
def avoir_transmettre_fne(id):
    """Déclenche la transmission de l'avoir <id> à la plateforme FNE."""
    if _get_perm('acces_avoirs') != 'ecriture':
        flash("Permission refusée.", "danger")
        return redirect(url_for('avoirs_list'))

    success, message, _data = transmettre_fne_avoir(id)
    if success:
        flash(f"📡 {message}", "success")
    else:
        flash(f"❌ FNE : {message}", "danger")
    return redirect(url_for('avoirs_list'))


@app.route('/factures')
@login_required
def factures_list():
    cfg = get_cfg()
    q = request.args.get('q','')
    statut_f = request.args.get('statut','')
    docs = _get_doc_vente('facture', q, statut_f)
    clients_all = query("SELECT id, nom, prenom, telephone, remise_pct FROM clients WHERE actif=1 ORDER BY nom")
    # Enrichir avec un label affichable dans le <select> du formulaire
    clients_all = [dict(c) for c in clients_all]
    for c in clients_all:
        c['label'] = (c['nom'] or '') + (' ' + c['prenom'] if c.get('prenom') else '') + (' — ' + c['telephone'] if c.get('telephone') else '')
    depots_all  = query("SELECT id, code, nom FROM depots WHERE actif=1")
    articles_all = query("""SELECT a.id, a.reference, a.designation, a.prix_vente_ht,
               a.tva, a.colisage, a.unite_vente,
               COALESCE(SUM(s.quantite_unite),0) as stock_total
               FROM articles a
               LEFT JOIN stocks s ON s.article_id=a.id
               WHERE a.actif=1
               GROUP BY a.id ORDER BY a.designation""")
    tva_def = float(cfg.get('tva_collectee_taux', cfg.get('tva', 0)) or 0)
    # KPIs
    kpi = query("""SELECT
        COALESCE(SUM(montant_paye),0)                                        as ca_total,
        COALESCE(SUM(CASE WHEN strftime('%Y-%m',date_doc)=strftime('%Y-%m','now') THEN montant_paye ELSE 0 END),0) as ca_mois,
        COALESCE(SUM(CASE WHEN date_doc=date('now') THEN montant_paye ELSE 0 END),0) as ca_jour,
        COUNT(*)                                                             as nb_total,
        COUNT(CASE WHEN statut IN ('en_attente','partielle') THEN 1 END)     as nb_attente
        FROM documents_vente WHERE type_doc='facture'""", one=True)
    # IDs des factures qui ont au moins un avoir lié (table peut ne pas exister encore)
    try:
        avoirs_facture_ids = query("SELECT DISTINCT facture_id FROM avoirs_clients WHERE facture_id IS NOT NULL")
        factures_avec_avoir = {row['facture_id'] for row in avoirs_facture_ids}
    except Exception:
        factures_avec_avoir = set()
    client_passager = query("SELECT id FROM clients WHERE code='CLI000' AND actif=1", one=True)
    passager_id = client_passager['id'] if client_passager else None
    return render_template('factures.html', cfg=cfg, docs=docs, q=q, statut_f=statut_f,
                           clients_all=clients_all, depots_all=depots_all,
                           articles=articles_all, articles_all=articles_all, tva_collectee=tva_def,
                           today=date.today().isoformat(),
                           ca_total=kpi['ca_total'], ca_mois=kpi['ca_mois'], ca_jour=kpi['ca_jour'],
                           nb_total=kpi['nb_total'], nb_attente=kpi['nb_attente'],
                           factures_avec_avoir=factures_avec_avoir,
                           passager_id=passager_id, edit_doc=None, edit_lignes=[])

@app.route('/documents_vente/add', methods=['POST'])
@login_required
def document_vente_add():
    f = request.form
    type_doc = f['type_doc']
    prefixes = {'devis':'DEV','commande':'CMD','facture':'FAC','livraison':'BL'}
    ref = next_ref(prefixes.get(type_doc,'DOC'))

    # Lignes JSON
    lignes = json.loads(f.get('lignes_json','[]'))
    tva_def = float(get_cfg().get('tva_collectee_taux', get_cfg().get('tva', 0)) or 0)  # TVA depuis paramètres
    total_ht = total_tva = total_ttc = 0
    for l in lignes:
        _col   = max(1, int(l.get('colisage', 1) or 1))
        _qte_u = float(l.get('qte_unite', l.get('quantite', 0)) or 0)
        _qte_c = float(l.get('qte_colis', l.get('colis', 0)) or 0)
        if _qte_c > 0 and _qte_u == 0:
            _qte_u = _qte_c * _col
        elif _qte_u > 0 and _qte_c == 0 and _col > 1:
            _qte_c = int(_qte_u // _col)
        _mode  = l.get('mode_saisie', 'unite')
        _rem   = float(l.get('remise', l.get('remise_pct', 0)) or 0)
        _tva   = tva_def                                     # ← toujours depuis paramètres
        # Priorite : montants deja calcules par le JS (ht_ligne / ttc_ligne).
        # Evite le decalage prix_colis vs prix_unitaire lors d'une edition.
        # Pour les BL (type_doc=livraison), les montants sont copies depuis la
        # commande et ne doivent jamais etre recalcules depuis prix_ht.
        _ht_js  = float(l.get('ht_ligne',  l.get('total_ht',  0)) or 0)
        _ttc_js = float(l.get('ttc_ligne', l.get('total_ttc', 0)) or 0)
        if _ht_js > 0:
            _ht   = round(_ht_js)
            _ttc  = round(_ttc_js) if _ttc_js > 0 else _ht
            _tvam = _ttc - _ht
        else:
            _base = _qte_c if _mode == 'colis' else _qte_u
            _prix = float(l.get('prix_ht', 0))
            _ht   = round(_base * _prix * (1 - _rem / 100))
            _tvam = round(_ht * _tva / 100)
            _ttc  = _ht + _tvam
        l['_qte_u'] = _qte_u; l['_qte_c'] = _qte_c
        l['_ht'] = _ht; l['_ttc'] = _ttc
        total_ht  += _ht
        total_tva += _tvam
        total_ttc += _ttc

    # ── Règles métier (stock + prix ≥ prix achat) ─────────────────────
    errs = _valider_regles_vente(lignes, type_doc, int(resolve_depot_id(f.get('depot_id'))))
    if errs:
        for e in errs:
            flash(e, 'danger')
        return redirect(request.referrer or url_for('factures_list'))

    # Remise globale
    remise = float(f.get('remise_globale',0) or 0)
    if remise:
        total_ht  *= (1 - remise/100)
        total_tva *= (1 - remise/100)
        total_ttc *= (1 - remise/100)

    doc_id = execute("""INSERT INTO documents_vente(type_doc,reference,client_id,depot_id,
                        date_doc,date_echeance,statut,remise_globale,
                        total_ht,total_tva,total_ttc,reste,mode_paiement,notes)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (type_doc, ref,
                      _safe_fk(f.get('client_id')),
                      resolve_depot_id(f.get('depot_id')),
                      f.get('date_doc', date.today().isoformat()),
                      f.get('date_echeance'),
                      f.get('statut','en_attente'),
                      remise, total_ht, total_tva, total_ttc, total_ttc,
                      f.get('mode_paiement','especes'),
                      f.get('notes')))

    for i, l in enumerate(lignes):
        execute("""INSERT INTO lignes_vente(document_id,article_id,designation,
                   quantite_unite,quantite_colis,prix_ht,remise_pct,tva,total_ht,total_ttc,num_ligne)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (doc_id, l.get('article_id') or l.get('stock_id') or None, l.get('designation',''),
                 l['_qte_u'], l['_qte_c'],
                 float(l.get('prix_ht', 0)),
                 float(l.get('remise', l.get('remise_pct', 0)) or 0),
                 tva_def,
                 l['_ht'], l['_ttc'], i+1))

    # Si facture payée directement
    if type_doc == 'facture' and f.get('montant_paye'):
        paye = float(f['montant_paye'])
        if paye > 0:
            rgl_ref = next_ref_rgl()
            execute("""INSERT INTO reglements(reference,source_type,source_id,client_id,
                       montant,mode_paiement,date_reglement)
                       VALUES(?,?,?,?,?,?,?)""",
                    (rgl_ref, 'facture', doc_id, f.get('client_id'),
                     paye, f.get('mode_paiement','especes'),
                     f.get('date_doc', date.today().isoformat())))
            reste = max(0, total_ttc - paye)
            statut_paye = 'reglee' if reste == 0 else 'partielle'
            execute("UPDATE documents_vente SET montant_paye=?,reste=?,statut=? WHERE id=?",
                    (paye, reste, statut_paye, doc_id))

    # ── Décrémentation du stock — factures et bons de livraison ────────
    # Logique identique au POS : déballage automatique colis → unités si
    # les unités libres sont insuffisantes. Centralisée dans le helper.
    if type_doc in ('facture', 'livraison'):
        _decrementer_stock_doc_vente(
            lignes, int(resolve_depot_id(f.get('depot_id'))),
            type_doc, doc_id, ref
        )

    flash(f"{type_doc.capitalize()} {ref} créé(e).", "success")
    routes = {'devis':'devis_list','commande':'commandes_vente_list','facture':'factures_list'}
    # ── Notification immédiate si facture impayée ──────────────────────
    if type_doc == 'facture':
        _fac_statut = query(
            "SELECT statut, reste FROM documents_vente WHERE id=?", (doc_id,), one=True)
        if _fac_statut and _fac_statut['statut'] != 'reglee' and (_fac_statut['reste'] or 0) > 0:
            import threading as _thr_nc
            _thr_nc.Thread(
                target=_notif_envoyer_creation_facture,
                args=(doc_id,),
                daemon=True
            ).start()
    return redirect(url_for(routes.get(type_doc,'factures_list')))

@app.route('/documents_vente/delete/<int:id>')
@login_required
def document_vente_delete(id):
    doc = query("SELECT * FROM documents_vente WHERE id=?", (id,), one=True)
    if doc:
        type_doc = doc['type_doc']
        statut   = doc['statut']
        ref      = doc['reference']

        # ── Blocage : devis déjà transformé en commande ─────────────────
        if type_doc == 'devis' and statut == 'converti':
            flash(f"Le devis {ref} a déjà été converti en commande — suppression impossible.",
                  "danger")
            return redirect(url_for('devis_list'))

        # ── Blocage : commande déjà transformée (facturée ou BL créé) ───
        if type_doc == 'commande' and statut in ('facturee', 'bl_cree'):
            motif = "facturée" if statut == 'facturee' else "transformée en bon de livraison"
            flash(f"La commande {ref} a déjà été {motif} — suppression impossible.",
                  "danger")
            return redirect(url_for('commandes_vente_list'))

        # ── Blocage suppression si facture réglée ou partiellement réglée ──
        if type_doc == 'facture':
            reglements_lies = query("""
                SELECT COUNT(*) as nb FROM reglements
                WHERE source_id=? AND source_type IN ('facture','vente')
            """, (id,), one=True)
            nb_rgl = reglements_lies['nb'] if reglements_lies else 0

            if statut == 'reglee' or nb_rgl > 0 or (doc['montant_paye'] or 0) > 0:
                if statut == 'reglee':
                    msg = (f"La facture {ref} est entièrement réglée — suppression impossible. "
                           f"Supprimez d'abord le(s) {nb_rgl} règlement(s) associé(s).")
                else:
                    msg = (f"La facture {ref} a {nb_rgl} règlement(s) partiel(s) — suppression impossible. "
                           f"Supprimez d'abord les règlements pour pouvoir supprimer cette facture.")
                flash(msg, "danger")
                return redirect(url_for('factures_list'))

        # ── Retour stock si facture en attente supprimée ───────────────
        # Couvre deux origines :
        #   1. Facture saisie directement → mouvements doc_type IN ('facture','pos','vente')
        #   2. Facture issue d'un BL      → mouvements doc_type='bon_livraison' du BL parent
        # Dans les deux cas on insère un mouvement compensatoire 'entree' pour
        # préserver la traçabilité (les sorties originales restent en base).
        nb_articles_restitues = 0
        if statut == 'en_attente':
            # Cas 1 : mouvements directs de la facture (saisie directe / POS)
            mvts = query("""SELECT id, article_id, depot_id, quantite_unite, prix_unitaire
                            FROM mouvements_stocks
                            WHERE doc_id=? AND type_mvt='sortie'
                              AND doc_type IN ('pos','facture','vente')""", (id,))

            # Cas 2 : facture issue d'un BL → mouvements 'bon_livraison' du BL parent
            bl_mvts = []
            bl_origine_id = doc['bl_origine_id'] if 'bl_origine_id' in doc.keys() else None
            if bl_origine_id:
                bl_mvts = query("""SELECT id, article_id, depot_id, quantite_unite, prix_unitaire
                                   FROM mouvements_stocks
                                   WHERE doc_id=? AND type_mvt='sortie'
                                     AND doc_type='bon_livraison'""", (bl_origine_id,))

            for m in list(mvts) + list(bl_mvts):
                aid = m['article_id']
                did = m['depot_id']
                qty = float(m['quantite_unite'] or 0)
                if not (aid and did and qty > 0):
                    continue
                # Réinjection en unités libres : le déballage colis→unités
                # à la vente n'est pas réversible physiquement (un carton
                # ouvert reste ouvert), donc on rend en unités.
                execute("""INSERT INTO stocks(article_id, depot_id, quantite_unite, quantite_colis)
                           VALUES(?,?,?,0) ON CONFLICT(article_id,depot_id)
                           DO UPDATE SET quantite_unite = quantite_unite + ?""",
                        (aid, did, qty, qty))
                # Stock total après réinjection (toutes formes, en unités)
                st = query("""SELECT COALESCE(s.quantite_unite,0) AS qu,
                                     COALESCE(s.quantite_colis,0) AS qc,
                                     COALESCE(a.colisage,1)       AS col
                              FROM stocks s
                              LEFT JOIN articles a ON a.id = s.article_id
                              WHERE s.article_id=? AND s.depot_id=?""",
                           (aid, did), one=True)
                stock_apres = (st['qu'] + st['qc'] * st['col']) if st else qty
                # Mouvement compensatoire pour la traçabilité
                execute("""INSERT INTO mouvements_stocks
                           (article_id, depot_id, type_mvt, quantite_unite, prix_unitaire,
                            doc_type, doc_id, doc_ref, stock_apres, operateur, notes)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (aid, did, 'entree', qty, float(m['prix_unitaire'] or 0),
                         'annulation', id, ref, stock_apres,
                         session.get('user') or 'POS',
                         f"Annulation facture {ref} — retour stock"))
                nb_articles_restitues += 1

        # ── Réactivation des documents AMONT (déverrouillage du bouton Supprimer) ──
        # Quand une facture est supprimée (= annulée dans cette appli), on remet le
        # BL / la commande dans un statut supprimable. Idem : supprimer une commande
        # « libère » le devis d'origine. Cela permet de dérouler la chaîne :
        #   facture → BL (re-livré) → commande (en attente) → devis (en attente).
        if type_doc == 'facture':
            bl_origine_id = doc['bl_origine_id'] if 'bl_origine_id' in doc.keys() else None
            parent_id     = doc['doc_parent_id'] if 'doc_parent_id' in doc.keys() else None

            if bl_origine_id:
                # Facture issue d'un BL → le BL repasse « Livré » (re-facturable/supprimable)
                execute("UPDATE bons_livraison SET statut='livre' "
                        "WHERE id=? AND statut='facture'", (bl_origine_id,))
                # La commande rattachée au BL repasse « bl_cree » (le BL existe encore ;
                # il faudra d'abord le supprimer pour rendre la commande supprimable).
                _bl_row = query("SELECT commande_id FROM bons_livraison WHERE id=?",
                                (bl_origine_id,), one=True)
                _cmd_id = (_bl_row['commande_id'] if _bl_row else None) or parent_id
                if _cmd_id:
                    execute("UPDATE documents_vente SET statut='bl_cree' "
                            "WHERE id=? AND type_doc='commande' AND statut='facturee'",
                            (_cmd_id,))
            elif parent_id:
                # Facture directe (sans BL) → la commande d'origine repasse « En attente »
                execute("UPDATE documents_vente SET statut='en_attente' "
                        "WHERE id=? AND type_doc='commande' AND statut='facturee'",
                        (parent_id,))

        if type_doc == 'commande':
            # Supprimer une commande libère le devis d'origine (re-convertible/supprimable)
            parent_id = doc['doc_parent_id'] if 'doc_parent_id' in doc.keys() else None
            if parent_id:
                execute("UPDATE documents_vente SET statut='en_attente' "
                        "WHERE id=? AND type_doc='devis' AND statut='converti'",
                        (parent_id,))

        # 1. Suppressions directes (tables avec ON DELETE CASCADE ou sans FK sortante)
        execute("DELETE FROM lignes_vente       WHERE document_id=?", (id,))
        execute("DELETE FROM reservations_stock WHERE commande_id=?", (id,))
        execute("DELETE FROM commissions        WHERE document_id=?", (id,))
        # Règlements : plusieurs variantes de colonnes possibles
        execute("DELETE FROM reglements WHERE source_id=? AND source_type='facture'", (id,))
        execute("DELETE FROM reglements WHERE source_id=? AND source_type='vente'",   (id,))
        # 2. Détachements (NULL) pour préserver les enregistrements liés
        execute("UPDATE avoirs_clients  SET facture_id=NULL  WHERE facture_id=?",  (id,))
        execute("UPDATE relances        SET facture_id=NULL  WHERE facture_id=?",  (id,))
        execute("UPDATE bons_livraison  SET facture_id=NULL  WHERE facture_id=?",  (id,))
        execute("UPDATE bons_livraison  SET commande_id=NULL WHERE commande_id=?", (id,))
        execute("UPDATE documents_vente SET doc_parent_id=NULL WHERE doc_parent_id=?", (id,))
        execute("UPDATE consignes       SET doc_vente_id=NULL WHERE doc_vente_id=?", (id,))
        # 3. Supprimer le document
        execute("DELETE FROM documents_vente WHERE id=?", (id,))
        if nb_articles_restitues:
            flash(f"Vente {ref} annulée — {nb_articles_restitues} article(s) retourné(s) en stock.",
                  "success")
        else:
            flash("Document supprimé.", "success")
        routes = {'devis':'devis_list','commande':'commandes_vente_list','facture':'factures_list'}
        return redirect(url_for(routes.get(type_doc, 'factures_list')))
    flash("Document introuvable.", "warning")
    return redirect(url_for('factures_list'))

# API lignes d'un document
@app.route('/api/doc_vente/<int:doc_id>/lignes')
@login_required
def api_doc_lignes(doc_id):
    lignes = query("""SELECT lv.*, a.reference, a.colisage FROM lignes_vente lv
                      LEFT JOIN articles a ON a.id=lv.article_id
                      WHERE lv.document_id=? ORDER BY lv.num_ligne""", (doc_id,))
    doc = query("SELECT * FROM documents_vente WHERE id=?", (doc_id,), one=True)
    return jsonify({'doc': dict(doc) if doc else {}, 'lignes': [dict(l) for l in lignes]})

@app.route('/api/achats/<int:doc_id>/lignes')
@login_required
def api_achat_lignes(doc_id):
    """API : lignes + entête d'une commande achat (pour aperçu impression)."""
    lignes = query("""SELECT la.*, a.reference
                      FROM lignes_achat la
                      LEFT JOIN articles a ON a.id=la.article_id
                      WHERE la.document_id=? ORDER BY la.id""", (doc_id,))
    doc = query("""SELECT da.*, f.nom as fourn_nom, f.telephone as fourn_tel,
                          f.adresse as fourn_adresse, f.email as fourn_email
                   FROM documents_achat da
                   LEFT JOIN fournisseurs f ON f.id=da.fournisseur_id
                   WHERE da.id=? AND da.type_doc='commande'""", (doc_id,), one=True)
    return jsonify({'doc': dict(doc) if doc else {}, 'lignes': [dict(l) for l in lignes]})

# ══════════════════════════════════════════════════════════════════════
#  DOCUMENTS ACHAT (Commandes fournisseurs)
# ══════════════════════════════════════════════════════════════════════

# ── Route impression DEVIS ──────────────────────────────────────────
@app.route('/devis/<int:id>/print')
@login_required
def devis_print(id):
    html = _print_document_vente(id, 'DEVIS')
    if isinstance(html, tuple): return html
    return _document_pdf_response(html, 'devis', id, f"Devis_{id}")


@app.route('/factures/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def facture_edit(id):
    """Modifier une facture (uniquement si non réglée / non annulée)."""
    fac = query("SELECT * FROM documents_vente WHERE id=? AND type_doc='facture'", (id,), one=True)
    if not fac:
        flash("Facture introuvable.", "danger")
        return redirect(url_for('factures_list'))
    if fac['statut'] in ('reglee', 'annule', 'annulee'):
        flash("Cette facture est réglée ou annulée et ne peut plus être modifiée.", "warning")
        return redirect(url_for('factures_list'))
    if (fac['montant_paye'] or 0) > 0:
        flash("Cette facture a déjà reçu un règlement partiel et ne peut plus être modifiée. "
              "Annulez d'abord les règlements pour la modifier.", "warning")
        return redirect(url_for('factures_list'))

    if request.method == 'POST':
        f = request.form
        lignes = json.loads(f.get('lignes_json', '[]'))
        tva_def = float(get_cfg().get('tva_collectee_taux', get_cfg().get('tva', 0)) or 0)
        total_ht = total_tva = total_ttc = 0
        for l in lignes:
            _col   = max(1, int(l.get('colisage', 1) or 1))
            _qte_u = float(l.get('qte_unite', l.get('quantite', 0)) or 0)
            _qte_c = float(l.get('qte_colis', l.get('colis', 0)) or 0)
            if _qte_c > 0 and _qte_u == 0:
                _qte_u = _qte_c * _col
            elif _qte_u > 0 and _qte_c == 0 and _col > 1:
                _qte_c = int(_qte_u // _col)
            _mode  = l.get('mode_saisie', 'unite')
            _rem   = float(l.get('remise', l.get('remise_pct', 0)) or 0)

            # Priorite : montants deja calcules par le JS (ht_ligne / ttc_ligne).
            # Evite le decalage prix_colis vs prix_unitaire lors d'une edition.
            # Recalcul depuis prix_ht uniquement si ht_ligne est absent ou nul.
            _ht_js  = float(l.get('ht_ligne',  l.get('total_ht',  0)) or 0)
            _ttc_js = float(l.get('ttc_ligne', l.get('total_ttc', 0)) or 0)
            if _ht_js > 0:
                _ht   = round(_ht_js)
                _ttc  = round(_ttc_js) if _ttc_js > 0 else _ht
                _tvam = _ttc - _ht
            else:
                _base = _qte_c if _mode == 'colis' else _qte_u
                _prix = float(l.get('prix_ht', 0))
                _ht   = round(_base * _prix * (1 - _rem / 100))
                _tvam = round(_ht * tva_def / 100)
                _ttc  = _ht + _tvam

            l['_qte_u'] = _qte_u; l['_qte_c'] = _qte_c
            l['_ht'] = _ht; l['_ttc'] = _ttc
            total_ht  += _ht
            total_tva += _tvam
            total_ttc += _ttc

        # ── Règles métier (prix ≥ prix achat + contrôle stock pour facture) ──
        # On annule d'abord les anciennes sorties stock liées à cette facture,
        # puis on valide. Si la validation échoue, on restaure l'ancien état.
        old_mvts, _ = _restituer_stock_doc_vente(
            id, doc_ref=fac['reference'], doc_types=('facture','livraison'),
            tracer=False  # pas de trace pour les retours temporaires de validation
        )
        errs = _valider_regles_vente(lignes, 'facture', int(resolve_depot_id(f.get('depot_id'))))
        if errs:
            # ROLLBACK : re-décrémenter pour restaurer l'état initial du stock
            for m in old_mvts:
                aid = m['article_id']; did = m['depot_id']
                qte_old = float(m['quantite_unite'] or 0)
                if not (aid and did and qte_old > 0):
                    continue
                execute("""UPDATE stocks SET quantite_unite = quantite_unite - ?
                           WHERE article_id=? AND depot_id=?""",
                        (qte_old, aid, did))
            for e in errs:
                flash(e, 'danger')
            return redirect(url_for('facture_edit', id=id))

        # Validation OK : on enregistre les mouvements 'entree' compensatoires
        # pour tracer l'annulation, puis on procédera au remplacement complet
        for m in old_mvts:
            aid = m['article_id']; did = m['depot_id']
            qte_old = float(m['quantite_unite'] or 0)
            if not (aid and did and qte_old > 0):
                continue
            st = query("""SELECT COALESCE(s.quantite_unite,0) AS qu,
                                 COALESCE(s.quantite_colis,0) AS qc,
                                 COALESCE(a.colisage,1)       AS col
                          FROM stocks s LEFT JOIN articles a ON a.id=s.article_id
                          WHERE s.article_id=? AND s.depot_id=?""", (aid, did), one=True)
            stock_apres = (st['qu'] + st['qc'] * st['col']) if st else qte_old
            execute("""INSERT INTO mouvements_stocks
                       (article_id, depot_id, type_mvt, quantite_unite, prix_unitaire,
                        stock_apres, doc_type, doc_id, doc_ref, operateur, notes)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (aid, did, 'entree', qte_old, float(m['prix_unitaire'] or 0),
                     stock_apres, 'modif_facture', id, fac['reference'],
                     session.get('user_nom') or 'FACTURE',
                     f"Annulation pour modification facture {fac['reference']}"))

        remise = float(f.get('remise_globale', 0) or 0)
        if remise:
            total_ht  *= (1 - remise / 100)
            total_tva *= (1 - remise / 100)
            total_ttc *= (1 - remise / 100)

        execute("""UPDATE documents_vente SET
                   client_id=?, depot_id=?, date_doc=?, date_echeance=?,
                   remise_globale=?, total_ht=?, total_tva=?, total_ttc=?,
                   reste=?, mode_paiement=?, notes=?
                   WHERE id=?""",
                (_safe_fk(f.get('client_id')),
                 resolve_depot_id(f.get('depot_id')),
                 f.get('date_doc', date.today().isoformat()),
                 f.get('date_echeance') or None,
                 remise, round(total_ht, 2), round(total_tva, 2), round(total_ttc, 2),
                 round(total_ttc, 2),
                 f.get('mode_paiement', 'especes'),
                 f.get('notes', ''),
                 id))

        # Remplacer les lignes
        execute("DELETE FROM lignes_vente WHERE document_id=?", (id,))
        for i, l in enumerate(lignes):
            execute("""INSERT INTO lignes_vente(document_id,article_id,designation,
                       quantite_unite,quantite_colis,prix_ht,remise_pct,tva,total_ht,total_ttc,num_ligne)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (id, l.get('article_id') or l.get('stock_id') or None, l.get('designation', ''),
                     l['_qte_u'], l['_qte_c'],
                     float(l.get('prix_ht', 0)),
                     float(l.get('remise', l.get('remise_pct', 0)) or 0),
                     tva_def, l['_ht'], l['_ttc'], i + 1))

        # ── Re-décrémentation du stock avec la nouvelle saisie ─────────────
        # (utilise la même logique que document_vente_add : déballage colis auto)
        _decrementer_stock_doc_vente(
            lignes,
            int(resolve_depot_id(f.get('depot_id'))),
            'facture', id, fac['reference']
        )

        # ── Encaissement immédiat si statut = 'reglee' ──────────────────────
        # Le formulaire envoie statut='reglee' + montant_paye=total_ttc via submitVente().
        # facture_edit ne bloquait pas l'accès si montant_paye=0, donc on traite ici.
        statut_form = (f.get('statut') or '').strip()
        montant_paye_form = float(f.get('montant_paye') or 0)
        if statut_form == 'reglee' and montant_paye_form > 0:
            # Supprimer les anciens règlements liés à cette facture (évite doublons si re-édition)
            execute("DELETE FROM reglements WHERE source_type='facture' AND source_id=?", (id,))
            rgl_ref = next_ref_rgl()
            execute("""INSERT INTO reglements(reference,source_type,source_id,client_id,
                       montant,mode_paiement,date_reglement)
                       VALUES(?,?,?,?,?,?,?)""",
                    (rgl_ref, 'facture', id,
                     _safe_fk(f.get('client_id')),
                     round(montant_paye_form, 2),
                     f.get('mode_paiement', 'especes'),
                     f.get('date_doc', date.today().isoformat())))
            reste_final = max(0, round(total_ttc, 2) - round(montant_paye_form, 2))
            statut_final = 'reglee' if reste_final == 0 else 'partielle'
            execute("UPDATE documents_vente SET montant_paye=?,reste=?,statut=? WHERE id=?",
                    (round(montant_paye_form, 2), reste_final, statut_final, id))
            # ── Reçu soldé si facture entièrement réglée ─────────────
            if statut_final == 'reglee':
                import threading as _thr_rs3
                _thr_rs3.Thread(
                    target=_notif_envoyer_recu_solde, args=(id,), daemon=True).start()
        elif statut_form == 'en_attente':
            # Réinitialiser si on repasse en attente (annulation paiement)
            execute("DELETE FROM reglements WHERE source_type='facture' AND source_id=?", (id,))
            execute("UPDATE documents_vente SET montant_paye=0,reste=?,statut='en_attente' WHERE id=?",
                    (round(total_ttc, 2), id))

        flash(f"Facture {fac['reference']} mise à jour.", "success")
        return redirect(url_for('factures_list'))

    # GET : préparer les données pour le formulaire d'édition
    cfg = get_cfg()
    lignes_db = query("""SELECT lv.*, a.reference as art_ref, a.colisage, a.unite_vente
                         FROM lignes_vente lv
                         LEFT JOIN articles a ON a.id=lv.article_id
                         WHERE lv.document_id=? ORDER BY lv.num_ligne""", (id,))
    clients_all  = query("SELECT id, nom, prenom, telephone FROM clients WHERE actif=1 ORDER BY nom")
    depots_all   = query("SELECT id, code, nom FROM depots WHERE actif=1")
    articles_all = query("""SELECT a.id, a.reference, a.designation, a.prix_vente_ht,
                            a.tva, a.colisage, a.unite_vente,
                            COALESCE(SUM(s.quantite_unite),0) as stock_total
                            FROM articles a
                            LEFT JOIN stocks s ON s.article_id=a.id
                            WHERE a.actif=1
                            GROUP BY a.id ORDER BY a.designation""")
    tva_def = float(cfg.get('tva_collectee_taux', cfg.get('tva', 0)) or 0)
    edit_doc_d    = dict(fac)
    edit_lignes_d = [dict(l) for l in lignes_db]
    clients_list  = [dict(c) for c in clients_all]
    for c in clients_list:
        c['label'] = (c['nom'] or '') + (' ' + c['prenom'] if c.get('prenom') else '') + (' — ' + c['telephone'] if c.get('telephone') else '')
    kpi = query("""SELECT
        COALESCE(SUM(montant_paye),0)                                        as ca_total,
        COALESCE(SUM(CASE WHEN strftime('%Y-%m',date_doc)=strftime('%Y-%m','now') THEN montant_paye ELSE 0 END),0) as ca_mois,
        COALESCE(SUM(CASE WHEN date_doc=date('now') THEN montant_paye ELSE 0 END),0) as ca_jour,
        COUNT(*)                                                             as nb_total,
        COUNT(CASE WHEN statut IN ('en_attente','partielle') THEN 1 END)     as nb_attente
        FROM documents_vente WHERE type_doc='facture'""", one=True)
    client_passager = query("SELECT id FROM clients WHERE code='CLI000' AND actif=1", one=True)
    passager_id = client_passager['id'] if client_passager else None
    return render_template('factures.html', cfg=cfg,
                           docs=[], q='', statut_f='',
                           clients_all=clients_list, depots_all=depots_all,
                           articles=articles_all, articles_all=articles_all,
                           tva_collectee=tva_def,
                           today=date.today().isoformat(),
                           ca_total=kpi['ca_total'], ca_mois=kpi['ca_mois'], ca_jour=kpi['ca_jour'],
                           nb_total=kpi['nb_total'], nb_attente=kpi['nb_attente'],
                           edit_doc=edit_doc_d, edit_lignes=edit_lignes_d,
                           passager_id=passager_id,
                           factures_avec_avoir=set())



@app.route('/commandes/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def commande_vente_edit(id):
    """Modifier une commande vente (uniquement si non facturée / non annulée)."""
    cmd = query("SELECT * FROM documents_vente WHERE id=? AND type_doc='commande'", (id,), one=True)
    if not cmd:
        flash("Commande introuvable.", "danger")
        return redirect(url_for('commandes_vente_list'))
    if cmd['statut'] in ('facturee', 'annulee'):
        flash("Cette commande a déjà été facturée ou annulée et ne peut plus être modifiée.", "warning")
        return redirect(url_for('commandes_vente_list'))

    if request.method == 'POST':
        f = request.form
        lignes = json.loads(f.get('lignes_json', '[]'))
        tva_def = float(get_cfg().get('tva_collectee_taux', get_cfg().get('tva', 0)) or 0)
        total_ht = total_tva = total_ttc = 0
        for l in lignes:
            _col   = max(1, int(l.get('colisage', 1) or 1))
            _qte_u = float(l.get('qte_unite', l.get('quantite', 0)) or 0)
            _qte_c = float(l.get('qte_colis', l.get('colis', 0)) or 0)
            if _qte_c > 0 and _qte_u == 0:
                _qte_u = _qte_c * _col
            elif _qte_u > 0 and _qte_c == 0 and _col > 1:
                _qte_c = int(_qte_u // _col)
            _mode  = l.get('mode_saisie', 'unite')
            _rem   = float(l.get('remise', l.get('remise_pct', 0)) or 0)
            # Priorite : montants deja calcules par le JS (ht_ligne / ttc_ligne).
            # Evite le decalage prix_colis vs prix_unitaire lors d'une edition.
            _ht_js  = float(l.get('ht_ligne',  l.get('total_ht',  0)) or 0)
            _ttc_js = float(l.get('ttc_ligne', l.get('total_ttc', 0)) or 0)
            if _ht_js > 0:
                _ht   = round(_ht_js)
                _ttc  = round(_ttc_js) if _ttc_js > 0 else _ht
                _tvam = _ttc - _ht
            else:
                _base = _qte_c if _mode == 'colis' else _qte_u
                _prix = float(l.get('prix_ht', 0))
                _ht   = round(_base * _prix * (1 - _rem / 100))
                _tvam = round(_ht * tva_def / 100)
                _ttc  = _ht + _tvam
            l['_qte_u'] = _qte_u; l['_qte_c'] = _qte_c
            l['_ht'] = _ht; l['_ttc'] = _ttc
            total_ht  += _ht
            total_tva += _tvam
            total_ttc += _ttc

        # ── Règles métier (prix ≥ prix achat ; stock non vérifié pour commande) ──
        errs = _valider_regles_vente(lignes, 'commande', int(resolve_depot_id(f.get('depot_id'))))
        if errs:
            for e in errs:
                flash(e, 'danger')
            return redirect(url_for('commande_vente_edit', id=id))

        remise = float(f.get('remise_globale', 0) or 0)
        if remise:
            total_ht  *= (1 - remise / 100)
            total_tva *= (1 - remise / 100)
            total_ttc *= (1 - remise / 100)

        execute("""UPDATE documents_vente SET
                   client_id=?, depot_id=?, date_doc=?, date_echeance=?,
                   remise_globale=?, total_ht=?, total_tva=?, total_ttc=?,
                   reste=?, mode_paiement=?, notes=?
                   WHERE id=?""",
                (_safe_fk(f.get('client_id')),
                 resolve_depot_id(f.get('depot_id')),
                 f.get('date_doc', date.today().isoformat()),
                 f.get('date_echeance') or None,
                 remise, round(total_ht, 2), round(total_tva, 2), round(total_ttc, 2),
                 round(total_ttc, 2),
                 f.get('mode_paiement', 'especes'),
                 f.get('notes', ''),
                 id))

        # Remplacer les lignes
        execute("DELETE FROM lignes_vente WHERE document_id=?", (id,))
        for i, l in enumerate(lignes):
            execute("""INSERT INTO lignes_vente(document_id,article_id,designation,
                       quantite_unite,quantite_colis,prix_ht,remise_pct,tva,total_ht,total_ttc,num_ligne)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (id, l.get('article_id') or l.get('stock_id') or None, l.get('designation', ''),
                     l['_qte_u'], l['_qte_c'],
                     float(l.get('prix_ht', 0)),
                     float(l.get('remise', l.get('remise_pct', 0)) or 0),
                     tva_def, l['_ht'], l['_ttc'], i + 1))

        flash(f"Commande {cmd['reference']} mise à jour.", "success")
        return redirect(url_for('commandes_vente_list'))

    # GET : préparer les données pour le formulaire d'édition
    cfg = get_cfg()
    lignes_db = query("""SELECT lv.*, a.reference as art_ref, a.colisage, a.unite_vente
                         FROM lignes_vente lv
                         LEFT JOIN articles a ON a.id=lv.article_id
                         WHERE lv.document_id=? ORDER BY lv.num_ligne""", (id,))
    clients_all  = query("SELECT id, nom, prenom, telephone FROM clients WHERE actif=1 ORDER BY nom")
    depots_all   = query("SELECT id, code, nom FROM depots WHERE actif=1")
    articles_all = query("""SELECT a.id, a.reference, a.designation, a.prix_vente_ht,
                            a.tva, a.colisage, a.unite_vente,
                            COALESCE(SUM(s.quantite_unite),0) as stock_total
                            FROM articles a
                            LEFT JOIN stocks s ON s.article_id=a.id
                            WHERE a.actif=1
                            GROUP BY a.id ORDER BY a.designation""")
    tva_def = float(cfg.get('tva_collectee_taux', cfg.get('tva', 0)) or 0)
    edit_doc_d    = dict(cmd)
    edit_lignes_d = [dict(l) for l in lignes_db]
    clients_list  = [dict(c) for c in clients_all]
    for c in clients_list:
        c['label'] = (c['nom'] or '') + (' ' + c['prenom'] if c.get('prenom') else '') + (' — ' + c['telephone'] if c.get('telephone') else '')
    kpi = query("""SELECT COUNT(*) as nb_total,
                   COALESCE(SUM(total_ttc),0) as ca_total,
                   COALESCE(SUM(CASE WHEN strftime('%Y-%m',date_doc)=strftime('%Y-%m','now') THEN total_ttc ELSE 0 END),0) as ca_mois,
                   COUNT(CASE WHEN statut NOT IN ('facturee','annulee') THEN 1 END) as nb_attente
                   FROM documents_vente WHERE type_doc='commande'""", one=True)
    return render_template('commandes_vente.html', cfg=cfg,
                           docs=[], q='', statut_f='',
                           clients_all=clients_list, depots_all=depots_all,
                           articles=articles_all, articles_all=articles_all,
                           tva_collectee=tva_def,
                           today=date.today().isoformat(),
                           ca_total=kpi['ca_total'], ca_mois=kpi['ca_mois'],
                           nb_total=kpi['nb_total'], nb_attente=kpi['nb_attente'],
                           edit_doc=edit_doc_d, edit_lignes=edit_lignes_d)

# ── Route impression COMMANDE VENTE ─────────────────────────────────
@app.route('/commandes_vente/<int:id>/print')
@login_required
def commande_vente_print(id):
    html = _print_document_vente(id, 'COMMANDE')
    if isinstance(html, tuple): return html
    return _document_pdf_response(html, 'commande', id, f"Commande_{id}")

# ── Route impression FACTURE ─────────────────────────────────────────
@app.route('/factures/<int:id>/print')
@login_required
def facture_print(id):
    html = _print_document_vente(id, 'FACTURE')
    if isinstance(html, tuple): return html
    return _document_pdf_response(html, 'facture', id, f"Facture_{id}")

# ── Génération HTML : COMMANDE ACHAT (réutilisée par impression directe) ──
def _print_document_achat(id):
    cfg = get_cfg()
    doc = query("""SELECT da.*, f.nom as fourn_nom, f.telephone as fourn_tel,
                          f.adresse as fourn_adresse
                   FROM documents_achat da
                   LEFT JOIN fournisseurs f ON f.id=da.fournisseur_id
                   WHERE da.id=?""", (id,), one=True)
    if not doc:
        return "Commande achat introuvable", 404
    doc = dict(doc)
    lignes = [dict(r) for r in query("""SELECT la.*,
                      COALESCE(la.designation, a.designation) as designation,
                      a.reference
                      FROM lignes_achat la
                      LEFT JOIN articles a ON a.id=la.article_id
                      WHERE la.document_id=? ORDER BY la.id""", (id,))]
    nom_soc = cfg.get('nom_depot', 'Mon Commerce')
    tel_soc = cfg.get('telephone', '')
    adr_soc = cfg.get('adresse', '')
    devise  = cfg.get('devise', 'FCFA')

    def fcfa(v):
        try: return f"{int(float(v or 0)):,}".replace(',', ' ')
        except: return '0'

    lignes_html = ''
    for l in lignes:
        ht      = float(l.get('total_ht') or 0)
        ttc     = float(l.get('total_ttc') or 0)
        tva_pct = float(l.get('tva') or 0)
        tva_mnt = round(ht * tva_pct / 100) if tva_pct else round(ttc - ht)
        qte     = int(float(l.get('quantite_unite') or 0))
        colis   = int(float(l.get('quantite_colis') or 0))
        prix_ht = l.get('prix_achat_ht') or l.get('prix_ht') or 0
        lignes_html += (
            '<tr>'
            f'<td>{l.get("designation") or "—"}</td>'
            f'<td style="text-align:center">{qte}</td>'
            f'<td style="text-align:center;color:#64748b;font-size:11px;">{colis if colis else "—"}</td>'
            f'<td style="text-align:right">{fcfa(prix_ht)} {devise}</td>'
            f'<td style="text-align:right">{fcfa(ht)} {devise}</td>'
            f'<td style="text-align:right">{fcfa(tva_mnt)} {devise}</td>'
            f'<td style="text-align:right;font-weight:700">{fcfa(ttc)} {devise}</td>'
            '</tr>'
        )

    statut_map = {'brouillon':'Brouillon','envoye':'Envoyée','recu':'Reçue',
                  'partiel':'Partielle','annule':'Annulée'}
    html = _print_page_html(
        titre='BON DE COMMANDE', reference=doc['reference'],
        date_doc=doc.get('date_doc',''), statut=statut_map.get(doc.get('statut',''), doc.get('statut','')),
        statut_ok=doc.get('statut') in ('recu',),
        tiers_label='Fournisseur', tiers_nom=doc.get('fourn_nom') or '—',
        tiers_tel=doc.get('fourn_tel',''), tiers_adresse=doc.get('fourn_adresse',''),
        nom_soc=nom_soc, tel_soc=tel_soc, adr_soc=adr_soc,
        lignes_html=lignes_html,
        col_headers=['Désignation','Qté U.','Qté Colis','PU HT','HT','TVA','TTC'],
        ht_total=fcfa(doc.get('total_ht')), tva_total=fcfa(doc.get('total_tva')),
        ttc_total=fcfa(doc.get('total_ttc')), reste=fcfa(doc.get('reste')),
        devise=devise, doc_statut=doc.get('statut','')
    )
    return html

# ── Route impression COMMANDE ACHAT ─────────────────────────────────
@app.route('/achats/<int:id>/print')
@login_required
def achat_print(id):
    html = _print_document_achat(id)
    if isinstance(html, tuple): return html
    return _document_pdf_response(html, 'achat', id, f"Commande_Achat_{id}")

# ── Générateur partagé documents vente ──────────────────────────────
# ══════════════════════════════════════════════════════════════════════
#  TEMPLATE D'IMPRESSION — Stocké en BDD, modifiable depuis Paramètres
#  Variables Jinja2 disponibles :
#   titre, reference, date_doc, statut, badge_bg, badge_clr
#   nom_soc, tel_soc, adr_soc
#   tiers_label, tiers_nom, tiers_tel, tiers_adresse
#   col_headers (liste), lignes_html (HTML pré-rendu)
#   ht_total, tva_total, ttc_total, reste, devise, reste_positif (bool)
#   fne_block (HTML pré-rendu — vide sauf factures certifiées DGI)
# ══════════════════════════════════════════════════════════════════════
_DEFAULT_PRINT_TEMPLATE = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8">
<title>{{ titre }} {{ reference }}</title>
<style>
/* Police 'Inter' chargée localement uniquement (repli Arial) — plus de fetch
   réseau Google Fonts qui ralentissait l'impression directe. */
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter','Segoe UI',Arial,sans-serif;font-size:12px;color:#1e293b;background:#f1f5f9;min-height:100vh;padding:32px 20px}
.page{background:white;max-width:820px;margin:0 auto;border-radius:16px;overflow:hidden;box-shadow:0 4px 32px rgba(0,0,0,.10)}

/* ── Barre de contrôle impression ── */
.no-print{max-width:820px;margin:0 auto 18px;display:flex;gap:10px;align-items:center}
.btn-print{display:inline-flex;align-items:center;gap:7px;padding:10px 22px;background:linear-gradient(135deg,#1e40af,#3b82f6);color:white;border:none;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;box-shadow:0 3px 12px rgba(59,130,246,.35);transition:all .18s}
.btn-print:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(59,130,246,.45)}
.btn-close{display:inline-flex;align-items:center;gap:6px;padding:10px 18px;background:white;color:#64748b;border:1.5px solid #e2e8f0;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;transition:all .18s}
.btn-close:hover{background:#f8fafc;border-color:#cbd5e1}

/* ── Header blanc ── */
.doc-header{background:white;padding:28px 32px 20px;display:flex;justify-content:space-between;align-items:flex-start;gap:20px;border-bottom:3px solid #1a3a6c}
.doc-header-left{display:flex;flex-direction:column;gap:8px}
.logo-wrap{display:flex;flex-direction:column;align-items:flex-start;gap:6px}
.logo-img{max-height:64px;max-width:160px;object-fit:contain}
.soc-name{font-size:18px;font-weight:900;color:#1a3a6c;letter-spacing:.3px}
.soc-sub{font-size:10px;color:#64748b;margin-top:2px;font-weight:500}
.doc-header-right{text-align:right;flex-shrink:0}
.doc-type-badge{display:inline-block;background:#1a3a6c;color:white;font-size:10px;font-weight:800;padding:4px 14px;border-radius:20px;letter-spacing:1px;text-transform:uppercase;margin-bottom:8px}
.doc-ref-num{font-size:22px;font-weight:900;color:#2563eb;letter-spacing:.5px;font-family:'Courier New',monospace}
.doc-date-line{font-size:10px;color:#64748b;margin-top:4px;font-weight:500}
.statut-badge{display:inline-block;padding:4px 12px;border-radius:20px;font-size:10px;font-weight:800;background:{{ badge_bg }};color:{{ badge_clr }};margin-top:8px;letter-spacing:.3px}

/* ── Corps du document ── */
.doc-body{padding:28px 32px}

/* ── Bloc parties ── */
.parties{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px}
.party-spacer{display:block}
.party-box{padding:14px 16px;background:#f8fafc;border-radius:10px;border:1px solid #e2e8f0;position:relative;overflow:hidden}
.party-box::before{content:'';position:absolute;top:0;left:0;width:3px;height:100%;background:linear-gradient(180deg,#1e40af,#3b82f6)}
.party-label{font-size:8px;font-weight:800;color:#94a3b8;text-transform:uppercase;letter-spacing:1.2px;margin-bottom:6px}
.party-name{font-size:13px;font-weight:800;color:#1e293b}
.party-info{font-size:10px;color:#64748b;margin-top:3px;line-height:1.5}

/* ── Séparateur section ── */
.section-title{font-size:9px;font-weight:800;color:#94a3b8;text-transform:uppercase;letter-spacing:1.2px;margin-bottom:10px;display:flex;align-items:center;gap:8px}
.section-title::after{content:'';flex:1;height:1px;background:#e2e8f0}

/* ── Tableau articles ── */
table{width:100%;border-collapse:collapse;margin-bottom:20px;font-size:11px}
thead tr{background:linear-gradient(135deg,#1a3a6c,#1e40af)}
thead th{padding:10px 12px;font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.8px;color:white}
thead th:first-child{border-radius:8px 0 0 8px}
thead th:last-child{border-radius:0 8px 8px 0}
tbody tr{border-bottom:1px solid #f1f5f9;transition:background .1s}
tbody tr:hover{background:#f8fafc}
tbody tr:last-child{border-bottom:none}
tbody td{padding:9px 12px;color:#334155;vertical-align:middle}
tbody td:first-child{font-weight:600;color:#1e293b}

/* ── Totaux ── */
.totaux-wrap{display:flex;justify-content:flex-end;margin-bottom:20px}
.totaux-box{min-width:280px;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden}
.totaux-row{display:flex;justify-content:space-between;padding:8px 16px;font-size:12px;border-bottom:1px solid #f1f5f9}
.totaux-row:last-child{border-bottom:none}
.totaux-row span:first-child{color:#64748b;font-weight:500}
.totaux-row span:last-child{font-weight:700;color:#1e293b}
.totaux-row.ttc{background:linear-gradient(135deg,#1a3a6c,#1e40af);color:white;font-size:14px;font-weight:900;padding:12px 16px;border-bottom:none}
.totaux-row.ttc span{color:white !important}
.totaux-row.reste{background:#fff5f5;border-top:2px solid #fecaca}
.totaux-row.reste span{color:#dc2626 !important;font-weight:800}

/* ── FNE block ── */
.fne-zone{margin-top:18px;border:2px solid #1a3a6c;border-radius:12px;overflow:hidden;background:white;page-break-inside:avoid}
.fne-header{background:linear-gradient(135deg,#1a3a6c,#2563eb);color:white;padding:10px 16px;display:flex;align-items:center;gap:12px}
.fne-logo{background:white;color:#1a3a6c;font-weight:900;font-size:14px;padding:5px 11px;border-radius:6px;letter-spacing:1.5px;flex-shrink:0;border:2px solid #fbbf24}
.fne-title{font-size:12px;font-weight:800;letter-spacing:.5px}
.fne-sub{font-size:9px;opacity:.85;margin-top:1px}
.fne-body{display:flex;gap:14px;padding:12px 16px;align-items:center}
.fne-qr{flex-shrink:0;text-align:center}
.fne-qr-cap{font-size:8px;color:#64748b;margin-top:3px;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
.fne-info{flex:1;display:flex;flex-direction:column;gap:5px}
.fne-row{display:flex;justify-content:space-between;align-items:baseline;padding:4px 0;border-bottom:1px dashed #e2e8f0;font-size:11px}
.fne-row:last-child{border-bottom:none}
.fne-k{font-size:9px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:.4px}
.fne-v{font-weight:700;color:#1e293b;text-align:right}
.fne-ref{font-family:'Courier New',monospace;font-size:13px;color:#1a3a6c;letter-spacing:.5px}

/* ── Footer ── */
.doc-footer{background:#f8fafc;border-top:1px solid #e2e8f0;padding:14px 32px;display:flex;justify-content:space-between;align-items:center}
.footer-left{font-size:9px;color:#94a3b8;font-weight:500}
.footer-right{font-size:9px;color:#cbd5e1;font-weight:500}

@media print{
  .no-print{display:none !important}
  body{background:white;padding:0}
  .page{border-radius:0;box-shadow:none}
  .doc-header{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  thead tr{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .totaux-row.ttc{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .party-box::before{-webkit-print-color-adjust:exact;print-color-adjust:exact}
}
/* ── Mode Noir & Blanc ── */
{% if _imp_nb %}
body, .page, .doc-header, .party-box, .totaux-row.ttc,
thead tr, .doc-footer {
  filter: grayscale(100%) !important;
  -webkit-filter: grayscale(100%) !important;
}
.doc-header { background: #f5f5f5 !important; }
.totaux-row.ttc { background: #333 !important; }
{% endif %}
</style></head><body>

<div class="no-print">
  <button class="btn-print" onclick="window.print()">🖨️ Imprimer</button>
  <button class="btn-close" onclick="window.close()">✕ Fermer</button>
</div>

<div class="page">
  <!-- ── Header ── -->
  <div class="doc-header">
    <div class="doc-header-left">
      <div class="logo-wrap">
        {% if logo_recu %}
          <img src="{{ logo_recu }}" alt="Logo" class="logo-img">
        {% endif %}
        <div>
          <div class="soc-name">{{ nom_soc }}</div>
          {% if adr_soc %}<div class="soc-sub">📍 {{ adr_soc }}</div>{% endif %}
          {% if tel_soc %}<div class="soc-sub">📞 {{ tel_soc }}</div>{% endif %}
          {% if email_soc %}<div class="soc-sub">✉️ {{ email_soc }}</div>{% endif %}
          {% if ncc_soc or rccm_soc %}<div class="soc-sub">{% if ncc_soc %}NCC : {{ ncc_soc }}{% endif %}{% if ncc_soc and rccm_soc %} · {% endif %}{% if rccm_soc %}RCCM : {{ rccm_soc }}{% endif %}</div>{% endif %}
        </div>
      </div>
    </div>
    <div class="doc-header-right">
      <div class="doc-type-badge">{{ titre }}</div>
      <div class="doc-ref-num">{{ reference }}</div>
      <div class="doc-date-line">📅 {{ date_doc }}</div>
      <div><span class="statut-badge">{{ statut }}</span></div>
    </div>
  </div>

  <!-- ── Corps ── -->
  <div class="doc-body">

    <!-- Parties : colonne gauche vide (émetteur retiré), Client à droite -->
    <div class="parties">
      <div class="party-spacer"></div>
      <div class="party-box">
        <div class="party-label">{{ tiers_label }}</div>
        <div class="party-name">{{ tiers_nom }}</div>
        {% if tiers_tel %}<div class="party-info">📞 {{ tiers_tel }}</div>{% endif %}
        {% if tiers_adresse %}<div class="party-info">{{ tiers_adresse }}</div>{% endif %}
        {{ tiers_extra_html | safe }}
      </div>
    </div>

    <!-- Tableau -->
    <div class="section-title">Détail des articles</div>
    <table>
      <thead><tr>
        {% for h in col_headers %}<th style="text-align:{% if loop.first %}left{% else %}right{% endif %}">{{ h }}</th>{% endfor %}
      </tr></thead>
      <tbody>{{ lignes_html | safe }}</tbody>
    </table>

    <!-- Totaux -->
    <div class="totaux-wrap">
      <div class="totaux-box">
        <div class="totaux-row"><span>Total HT</span><span>{{ ht_total }} {{ devise }}</span></div>
        <div class="totaux-row"><span>TVA</span><span>{{ tva_total }} {{ devise }}</span></div>
        <div class="totaux-row ttc"><span>TOTAL TTC</span><span>{{ ttc_total }} {{ devise }}</span></div>
        {{ versements_html | safe }}
        {% if reste_positif %}
        <div class="totaux-row reste"><span>Reste à payer</span><span>{{ reste }} {{ devise }}</span></div>
        {% endif %}
      </div>
    </div>

    {{ fne_block | safe }}
    {{ extra_html | safe }}

  </div>

  <!-- ── Footer ── -->
  <div class="doc-footer">
    <div class="footer-left">{{ nom_soc }}{% if ncc_soc %} · NCC : {{ ncc_soc }}{% endif %}{% if rccm_soc %} · RCCM : {{ rccm_soc }}{% endif %}</div>
    <div class="footer-right">Document généré par DISTRIGEST · STiNAUG TECHNOLOGIE</div>
  </div>

</div>
</body></html>"""


def _fne_qr_svg(url, size_px=110):
    """
    Génère un QR code en SVG inline pour la FNE (DGI Côte d'Ivoire).
    Utilise la lib `qrcode` (pip install qrcode) si disponible, sinon retourne
    un fallback HTML avec un message d'installation.

    L'URL passée doit être le `token` retourné par l'API FNE
    (ex: http://54.247.95.108/fr/verification/019465c1-...).
    """
    if not url:
        return '<div style="font-size:9px;color:#94a3b8;font-style:italic;">QR code non disponible</div>'
    try:
        import qrcode
        import qrcode.image.svg
        # SvgPathImage = SVG pur (pas de Pillow nécessaire)
        factory = qrcode.image.svg.SvgPathImage
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10, border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(image_factory=factory)
        svg = img.to_string().decode('utf-8') if isinstance(img.to_string(), bytes) else img.to_string()
        # Forcer la taille du SVG (override des attributs natifs)
        import re as _re
        svg = _re.sub(r'<svg([^>]*)\swidth="[^"]*"', r'<svg\1', svg, count=1)
        svg = _re.sub(r'<svg([^>]*)\sheight="[^"]*"', r'<svg\1', svg, count=1)
        svg = svg.replace('<svg ',
            f'<svg width="{size_px}" height="{size_px}" style="display:block;background:white;" ', 1)
        return svg
    except ImportError:
        # Fallback : pas de QR code, mais URL en texte lisible
        return (f'<div style="width:{size_px}px;height:{size_px}px;border:1px dashed #cbd5e1;'
                f'border-radius:6px;display:flex;flex-direction:column;align-items:center;'
                f'justify-content:center;font-size:9px;color:#64748b;text-align:center;'
                f'padding:6px;line-height:1.4;">'
                f'<div style="font-size:18px;margin-bottom:4px;">📱</div>'
                f'<div style="font-weight:700;">QR indisponible</div>'
                f'<div style="font-size:8px;color:#94a3b8;margin-top:3px;">'
                f'<code>pip install qrcode</code></div></div>')
    except Exception as _e:
        return (f'<div style="width:{size_px}px;height:{size_px}px;border:1px dashed #fca5a5;'
                f'background:#fef2f2;border-radius:6px;display:flex;align-items:center;'
                f'justify-content:center;font-size:9px;color:#dc2626;text-align:center;padding:6px;">'
                f'Erreur QR : {str(_e)[:60]}</div>')


def _print_document_vente(id, titre):
    cfg = get_cfg()
    doc = query("""SELECT dv.*, c.nom as client_nom, c.prenom as client_prenom,
                          c.code as client_code, c.telephone as client_tel,
                          c.telephone2 as client_tel2, c.email as client_email,
                          c.adresse as client_adresse, c.ville as client_ville,
                          c.type_client as client_type
                   FROM documents_vente dv
                   LEFT JOIN clients c ON c.id=dv.client_id
                   WHERE dv.id=?""", (id,), one=True)
    if not doc:
        return f"{titre} introuvable", 404
    doc = dict(doc)
    lignes = [dict(r) for r in query("""SELECT lv.*,
                      COALESCE(lv.designation, a.designation) as designation,
                      a.reference
                      FROM lignes_vente lv
                      LEFT JOIN articles a ON a.id=lv.article_id
                      WHERE lv.document_id=? ORDER BY lv.num_ligne""", (id,))]
    nom_soc = cfg.get('nom_depot', 'Mon Commerce')
    tel_soc = cfg.get('telephone', '')
    adr_soc = cfg.get('adresse', '')
    devise  = cfg.get('devise', 'FCFA')

    def fcfa(v):
        try: return f"{int(float(v or 0)):,}".replace(',', ' ')
        except: return '0'

    lignes_html = ''
    for l in lignes:
        ht   = float(l.get('total_ht') or 0)
        ttc  = float(l.get('total_ttc') or 0)
        tva_pct = float(l.get('tva') or 0)
        tva_mnt = round(ht * tva_pct / 100) if tva_pct else round(ttc - ht)
        qte   = int(float(l.get('quantite_unite') or l.get('qte_unite') or 0))
        colis = int(float(l.get('quantite_colis') or 0))
        lignes_html += (
            '<tr>'
            f'<td>{l.get("designation") or "—"}</td>'
            f'<td style="text-align:center">{qte}</td>'
            f'<td style="text-align:center;color:#64748b;font-size:11px;">{colis if colis else "—"}</td>'
            f'<td style="text-align:right">{fcfa(l.get("prix_ht"))} {devise}</td>'
            f'<td style="text-align:right">{int(l.get("remise_pct") or 0)}%</td>'
            f'<td style="text-align:right">{fcfa(ht)} {devise}</td>'
            f'<td style="text-align:right">{fcfa(tva_mnt)} {devise}</td>'
            f'<td style="text-align:right;font-weight:700">{fcfa(ttc)} {devise}</td>'
            '</tr>'
        )

    statut_map = {'en_attente':'En attente','partielle':'Part. réglée',
                  'reglee':'Réglée','annule':'Annulée',
                  'brouillon':'Brouillon','valide':'Validée',
                  'livre':'Livrée','facture':'Facturée'}

    # ── Versements ─────────────────────────────────────────────────────
    reglements_v = [dict(r) for r in query(
        """SELECT montant, mode_paiement, date_reglement
           FROM reglements
           WHERE source_type IN ('facture','vente') AND source_id=?
           ORDER BY date_reglement, id""", (id,))]
    mode_labels = {
        'especes':'Espèces','wave':'Wave','orange_money':'Orange Money',
        'mtn_money':'MTN Money','moov_money':'Moov Money',
        'carte_bancaire':'Carte','virement':'Virement','cheque':'Chèque'
    }
    versements_html = ''
    if reglements_v:
        for r in reglements_v:
            mode = mode_labels.get(r['mode_paiement'], r['mode_paiement'] or '')
            if r['date_reglement']:
                parts = r['date_reglement'].split('-')
                date_fmt = f"{parts[2]}-{parts[1]}-{parts[0][2:]}"
            else:
                date_fmt = ''
            label = "✅" + (f" {mode}" if mode else '') + (f" · {date_fmt}" if date_fmt else '')
            versements_html += (
                f'<div class="totaux-row" style="background:#f0fdf4;border-top:1px solid #d1fae5;">'
                f'<span style="color:#15803d;font-weight:600;font-size:11px;">{label}</span>'
                f'<span style="color:#15803d;font-weight:800;">{fcfa(r["montant"])} {devise}</span>'
                f'</div>'
            )
    # ── Informations client complètes pour le bloc « Client » ──────────
    # Nom complet (nom + prénom)
    client_nom_complet = doc.get('client_nom') or 'Client passager'
    if doc.get('client_prenom'):
        client_nom_complet = f"{client_nom_complet} {doc['client_prenom']}"
    # Adresse complète (adresse + ville)
    _adr_parts = [p for p in (doc.get('client_adresse'), doc.get('client_ville')) if p]
    client_adresse_full = ', '.join(_adr_parts)
    # Lignes d'information supplémentaires (code, 2e tél, email, type)
    _type_labels = {'particulier':'Particulier','entreprise':'Entreprise',
                    'revendeur':'Revendeur','grossiste':'Grossiste',
                    'caisse':'Caisse'}
    _extra = []
    if doc.get('client_code'):
        _extra.append(f'<div class="party-info">N° client : {doc["client_code"]}</div>')
    if doc.get('client_tel2'):
        _extra.append(f'<div class="party-info">📱 {doc["client_tel2"]}</div>')
    if doc.get('client_email'):
        _extra.append(f'<div class="party-info">✉️ {doc["client_email"]}</div>')
    if doc.get('client_type'):
        _extra.append(f'<div class="party-info">{_type_labels.get(doc["client_type"], doc["client_type"])}</div>')
    tiers_extra_html = ''.join(_extra)

    return _print_page_html(
        titre=titre, reference=doc['reference'],
        date_doc=doc.get('date_doc',''),
        statut=statut_map.get(doc.get('statut',''), doc.get('statut','')),
        statut_ok=doc.get('statut') in ('reglee','livre','facture'),
        tiers_label='Client', tiers_nom=client_nom_complet,
        tiers_tel=doc.get('client_tel',''), tiers_adresse=client_adresse_full,
        tiers_extra_html=tiers_extra_html,
        nom_soc=nom_soc, tel_soc=tel_soc, adr_soc=adr_soc,
        lignes_html=lignes_html,
        col_headers=['Désignation','Qté U.','Qté Colis','PU HT','Rem.','HT','TVA','TTC'],
        ht_total=fcfa(doc.get('total_ht')), tva_total=fcfa(doc.get('total_tva')),
        ttc_total=fcfa(doc.get('total_ttc')), reste=fcfa(doc.get('reste')),
        devise=devise, doc_statut=doc.get('statut',''),
        # ── Infos FNE (DGI Côte d'Ivoire) — uniquement renseignées pour factures certifiées ──
        fne_statut=doc.get('fne_statut'),
        fne_reference=doc.get('fne_reference'),
        fne_qr_token=doc.get('fne_qr_token'),
        fne_date_transmission=doc.get('fne_date_transmission'),
        ncc_emetteur=cfg.get('ncc'),
        versements_html=versements_html,
    )

# ── Lancement automatique de l'impression (plus d'aperçu manuel) ────
_AUTOPRINT_SNIPPET = (
    "<script>window.addEventListener('load',function(){"
    "setTimeout(function(){window.print();},250);});</script>"
)

def _inject_autoprint(html):
    """Injecte le déclenchement automatique de window.print() dans une page
    d'impression, juste avant </body> (sinon en fin de document)."""
    if not html:
        return html
    if _AUTOPRINT_SNIPPET in html:
        return html
    idx = html.lower().rfind('</body>')
    if idx != -1:
        return html[:idx] + _AUTOPRINT_SNIPPET + html[idx:]
    return html + _AUTOPRINT_SNIPPET

# ── Format d'impression : A4 / A5 / A6 / A7 / 80mm (ticket thermique) ─────
def _norm_print_format(fmt):
    """Normalise une valeur de format vers : 'a4', 'a5', 'a6', 'a7' ou '80mm'."""
    f = (fmt or 'a4').strip().lower()
    if f in ('80mm', '80', 'thermique', 'thermal', 'ticket', 'recu', 'reçu',
             'rouleau', 'rouleau80'):
        return '80mm'
    if f == 'a5':
        return 'a5'
    if f == 'a6':
        return 'a6'
    if f == 'a7':
        return 'a7'
    return 'a4'

def _current_print_format():
    """Format effectif : paramètre d'URL ?format= prioritaire, sinon réglage
    'imprimante_type' des paramètres, sinon A4."""
    f = None
    try:
        f = request.args.get('format')
    except Exception:
        f = None
    if not f:
        try:
            cfg = get_cfg()
            # format_doc est la clé canonique (onglet infos) ;
            # imprimante_type est l'ancienne clé (onglet imprimante) — fallback
            f = cfg.get('format_doc') or cfg.get('imprimante_type')
        except Exception:
            f = None
    return _norm_print_format(f)

def _print_format_css(fmt, document=False):
    """Bloc <style> qui fixe la taille de page (@page) et, pour le 80mm,
    adapte la mise en page en colonne étroite type ticket de caisse.
    `document=True` active les surcharges propres au gabarit des documents
    de vente/achat (classes .page, .doc-header, .parties, etc.)."""
    fmt = _norm_print_format(fmt)

    if fmt == '80mm':
        css = (
            "@page { size: 80mm auto; margin: 3mm; }"
            "html,body{background:#fff !important;}"
            "body{width:72mm !important;margin:0 auto !important;padding:0 !important;"
            "font-size:10px !important;}"
            ".no-print{max-width:72mm !important;margin:0 auto 8px !important;}"
            "table{font-size:9px !important;}"
        )
        if document:
            css += (
                ".page{max-width:72mm !important;width:72mm !important;"
                "border-radius:0 !important;box-shadow:none !important;margin:0 auto !important;}"
                ".doc-header{flex-direction:column !important;align-items:center !important;"
                "text-align:center !important;gap:6px !important;padding:8px 6px !important;}"
                ".doc-header-right{text-align:center !important;}"
                ".doc-body{padding:8px 6px !important;}"
                ".parties{grid-template-columns:1fr !important;gap:6px !important;"
                "margin-bottom:10px !important;}"
                "thead th,tbody td{padding:3px 4px !important;}"
                ".totaux-wrap{margin-bottom:10px !important;}"
                ".totaux-box{min-width:0 !important;width:100% !important;}"
                ".doc-footer{flex-direction:column !important;gap:3px !important;"
                "text-align:center !important;padding:8px 6px !important;}"
                ".soc-name{font-size:13px !important;}"
                ".doc-ref-num{font-size:15px !important;}"
                ".fne-body{flex-direction:column !important;align-items:center !important;}"
                ".fne-zone{page-break-inside:avoid;}"
                ".totaux-wrap{display:block !important;}"
                ".totaux-row{display:flex !important;justify-content:space-between !important;padding:5px 8px !important;font-size:11px !important;border-bottom:1px solid #e2e8f0 !important;}"
                ".totaux-row span:first-child{color:#64748b !important;font-weight:500 !important;}"
                ".totaux-row span:last-child{font-weight:700 !important;color:#1e293b !important;}"
                ".totaux-row.ttc{display:flex !important;justify-content:space-between !important;background:#1a3a6c !important;color:#fff !important;font-size:13px !important;font-weight:900 !important;padding:8px !important;margin-top:2px !important;border-radius:4px !important;-webkit-print-color-adjust:exact !important;print-color-adjust:exact !important;}"
                ".totaux-row.ttc span{color:#fff !important;font-size:13px !important;font-weight:900 !important;}"
                ".totaux-row.reste{display:flex !important;justify-content:space-between !important;background:#fff5f5 !important;border-top:2px solid #fecaca !important;padding:6px 8px !important;-webkit-print-color-adjust:exact !important;print-color-adjust:exact !important;}"
                ".totaux-row.reste span{color:#dc2626 !important;font-weight:800 !important;}"
            )
        return f'<style id="dg-print-format" data-fmt="80mm">{css}</style>'

    _sizes = {'a4': ('A4', '12mm'), 'a5': ('A5', '10mm'), 'a6': ('A6', '8mm'), 'a7': ('A7', '6mm')}
    page_size, margin = _sizes[fmt]
    css = "@page { size: %s; margin: %s; }" % (page_size, margin)
    return f'<style id="dg-print-format" data-fmt="{fmt}">{css}</style>'

def _inject_print_format(html, css_block):
    """Insère le bloc de format dans le <head> (sinon avant </body>) pour qu'il
    surcharge le CSS du gabarit. Idempotent (ne réinjecte pas)."""
    if not html or not css_block:
        return html
    if 'dg-print-format' in html:
        return html
    idx = html.lower().find('</head>')
    if idx != -1:
        return html[:idx] + css_block + html[idx:]
    idx = html.lower().rfind('</body>')
    if idx != -1:
        return html[:idx] + css_block + html[idx:]
    return css_block + html

# ── Template HTML générique impression ──────────────────────────────
def _infos_entreprise():
    """Coordonnées complètes de l'entreprise (Paramètres → Infos société),
    utilisées par TOUS les documents d'impression :
    adresse complète, téléphone, email, NCC, N° RCCM."""
    c = get_cfg()
    adr   = (c.get('adresse') or '').strip()
    ville = (c.get('ville') or '').strip()
    adresse_complete = ', '.join(x for x in (adr, ville) if x)
    return {
        'nom':     c.get('nom_depot') or c.get('entreprise_nom') or 'DISTRIGEST',
        'adresse': adresse_complete,
        'tel':     (c.get('telephone') or '').strip(),
        'email':   (c.get('email') or '').strip(),
        'ncc':     (c.get('ncc') or '').strip(),
        'rccm':    (c.get('rccm') or '').strip(),
    }


def _print_page_html(titre, reference, date_doc, statut, statut_ok,
                     tiers_label, tiers_nom, tiers_tel, tiers_adresse,
                     nom_soc, tel_soc, adr_soc, lignes_html,
                     col_headers, ht_total, tva_total, ttc_total,
                     reste, devise, doc_statut,
                     # ── Paramètres FNE optionnels (uniquement pour factures certifiées) ──
                     fne_statut=None, fne_reference=None, fne_qr_token=None,
                     fne_date_transmission=None, ncc_emetteur=None,
                     extra_html='', montant_paye=0, versements_html='',
                     tiers_extra_html=''):
    badge_bg  = '#dcfce7' if statut_ok else '#fef9c3'
    badge_clr = '#15803d' if statut_ok else '#a16207'

    # ── Mode impression couleur / N&B ──
    _imp_nb = (get_cfg().get('impression_couleur', 'couleur') == 'nb')

    # ── Coordonnées complètes de l'entreprise (adresse, tél, email, NCC, RCCM) ──
    _soc = _infos_entreprise()
    adr_soc   = _soc['adresse'] or (adr_soc or '')
    tel_soc   = tel_soc or _soc['tel']
    email_soc = _soc['email']
    ncc_soc   = _soc['ncc']
    rccm_soc  = _soc['rccm']

    # ── Reste à payer : booléen pour le template Jinja2 ──
    reste_positif = False
    try:
        reste_positif = float(str(reste).replace(' ', '')) > 0
    except Exception:
        pass
    # Formater montant_paye pour le template
    try:
        montant_paye_fmt = f"{int(float(montant_paye or 0)):,}".replace(',', ' ')
    except Exception:
        montant_paye_fmt = '0'
    montant_paye = float(montant_paye or 0)

    # ── Bloc FNE (uniquement pour factures certifiées par la DGI) ──
    fne_block = ''
    if fne_statut == 'certifiee' and fne_reference:
        qr_url = fne_qr_token or ''
        qr_svg = _fne_qr_svg(qr_url, size_px=110)
        ncc_row = ''
        if ncc_emetteur:
            ncc_row = (f'<div class="fne-row"><span class="fne-k">NCC émetteur</span>'
                       f'<span class="fne-v">{ncc_emetteur}</span></div>')
        date_row = ''
        if fne_date_transmission:
            date_row = (f'<div class="fne-row"><span class="fne-k">Date certification</span>'
                        f'<span class="fne-v">{fne_date_transmission}</span></div>')
        url_short = qr_url if len(qr_url) < 60 else qr_url[:55] + '…'
        fne_block = f"""
<div class="fne-zone">
  <div class="fne-header">
    <div class="fne-logo">FNE</div>
    <div>
      <div class="fne-title">FACTURE NORMALISÉE ÉLECTRONIQUE</div>
      <div class="fne-sub">Certifiée par la Direction Générale des Impôts · République de Côte d'Ivoire</div>
    </div>
  </div>
  <div class="fne-body">
    <div class="fne-qr">
      {qr_svg}
      <div class="fne-qr-cap">Scanner pour vérifier</div>
    </div>
    <div class="fne-info">
      <div class="fne-row"><span class="fne-k">N° fiscal DGI</span><span class="fne-v fne-ref">{fne_reference}</span></div>
      {ncc_row}
      {date_row}
      <div class="fne-row"><span class="fne-k">Vérification</span>
        <span class="fne-v" style="font-family:monospace;font-size:9px;color:#1d4ed8;word-break:break-all;">{url_short}</span></div>
    </div>
  </div>
</div>
"""

    # ── Charger le template Jinja2 depuis la BDD (avec fallback constante) ──
    from jinja2 import Template
    template_str = get_cfg().get('template_print_html') or _DEFAULT_PRINT_TEMPLATE
    logo_recu = get_cfg().get('logo_recu', '')
    _fmt_css = _print_format_css(_current_print_format(), document=True)
    try:
        return _inject_autoprint(_inject_print_format(Template(template_str).render(
            titre=titre, reference=reference, date_doc=date_doc,
            statut=statut, badge_bg=badge_bg, badge_clr=badge_clr,
            _imp_nb=_imp_nb,
            nom_soc=nom_soc, tel_soc=tel_soc, adr_soc=adr_soc,
            email_soc=email_soc, ncc_soc=ncc_soc, rccm_soc=rccm_soc,
            tiers_label=tiers_label, tiers_nom=tiers_nom,
            tiers_tel=tiers_tel, tiers_adresse=tiers_adresse,
            tiers_extra_html=tiers_extra_html,
            col_headers=col_headers, lignes_html=lignes_html,
            ht_total=ht_total, tva_total=tva_total, ttc_total=ttc_total,
            reste=reste, devise=devise, reste_positif=reste_positif,
            fne_block=fne_block, extra_html=extra_html,
            montant_paye=montant_paye,
            versements_html=versements_html,
            logo_recu=logo_recu,
        ), _fmt_css))
    except Exception as e:
        # Si le template personnalisé est invalide, on retombe sur le défaut
        return _inject_autoprint(_inject_print_format(Template(_DEFAULT_PRINT_TEMPLATE).render(
            titre=titre, reference=reference, date_doc=date_doc,
            statut=f"{statut} — ⚠️ Erreur template : {e}",
            badge_bg='#fee2e2', badge_clr='#dc2626',
            nom_soc=nom_soc, tel_soc=tel_soc, adr_soc=adr_soc,
            email_soc=email_soc, ncc_soc=ncc_soc, rccm_soc=rccm_soc,
            tiers_label=tiers_label, tiers_nom=tiers_nom,
            tiers_tel=tiers_tel, tiers_adresse=tiers_adresse,
            tiers_extra_html=tiers_extra_html,
            col_headers=col_headers, lignes_html=lignes_html,
            ht_total=ht_total, tva_total=tva_total, ttc_total=ttc_total,
            reste=reste, devise=devise, reste_positif=reste_positif,
            fne_block=fne_block, extra_html=extra_html,
            montant_paye=montant_paye,
            versements_html=versements_html,
            logo_recu=logo_recu,
        ), _fmt_css))

@app.route('/achats')
@login_required
def achats_list():
    cfg = get_cfg()
    q = request.args.get('q', '')
    statut_f = request.args.get('statut', '')
    sql = """SELECT da.*, f.nom as fourn_nom FROM documents_achat da
             LEFT JOIN fournisseurs f ON f.id=da.fournisseur_id
             WHERE da.type_doc='commande'"""
    args = []
    if q:
        sql += " AND (da.reference LIKE ? OR f.nom LIKE ?)"
        args += [f'%{q}%', f'%{q}%']
    if statut_f:
        sql += " AND da.statut=?"
        args.append(statut_f)
    sql += " ORDER BY da.date_creation DESC"
    docs = query(sql, args)
    fournisseurs_all = query("SELECT id, nom, telephone FROM fournisseurs WHERE actif=1 ORDER BY nom")
    depots_all = query("SELECT id, code, nom FROM depots WHERE actif=1")
    articles_all = query("""SELECT a.id, a.reference, a.designation, a.prix_achat_ht,
               a.tva, a.colisage, a.unite_vente,
               COALESCE(SUM(s.quantite_unite),0) as stock_total
               FROM articles a
               LEFT JOIN stocks s ON s.article_id=a.id
               WHERE a.actif=1
               GROUP BY a.id ORDER BY a.designation""")
    # KPIs — Commandes achat
    kpi_cmd = query("""SELECT
        COUNT(*)                                                              as nb_total,
        COALESCE(SUM(total_ttc),0)                                           as montant_total,
        COALESCE(SUM(CASE WHEN strftime('%Y-%m',date_doc)=strftime('%Y-%m','now') THEN total_ttc ELSE 0 END),0) as montant_mois,
        COUNT(CASE WHEN statut = 'en_attente' THEN 1 END) as nb_attente,
        COUNT(CASE WHEN statut = 'recu'       THEN 1 END) as nb_recues
        FROM documents_achat WHERE type_doc='commande'""", one=True)
    return render_template('commandes_achats.html', cfg=cfg, docs=docs, q=q, statut_f=statut_f,
                           fournisseurs_all=fournisseurs_all, depots_all=depots_all,
                           articles_all=articles_all, today=date.today().isoformat(),
                           # KPI communs (pour compatibilité template)
                           ca_total=kpi_cmd['montant_total'], ca_mois=kpi_cmd['montant_mois'],
                           nb_total=kpi_cmd['nb_total'],      nb_attente=kpi_cmd['nb_attente'],
                           nb_recues=kpi_cmd['nb_recues'],
                           nb_devis=0, montant_devis=0, nb_devis_actifs=0)

@app.route('/achats/add', methods=['POST'])
@login_required
def achat_add():
    f = request.form
    ref = next_ref_achat('CA')
    lignes = json.loads(f.get('lignes_json','[]'))
    tva_def = float(get_cfg().get('tva_collectee_taux', get_cfg().get('tva', 0)) or 0)  # TVA depuis paramètres
    total_ht = total_tva = total_ttc = 0
    for l in lignes:
        _col   = max(1, int(l.get('colisage', 1) or 1))
        _qte_u = float(l.get('qte_unite', l.get('quantite', 0)) or 0)
        _qte_c = float(l.get('qte_colis', l.get('colis', 0)) or 0)
        if _qte_c > 0 and _qte_u == 0:
            _qte_u = _qte_c * _col
        elif _qte_u > 0 and _qte_c == 0 and _col > 1:
            _qte_c = int(_qte_u // _col)
        _mode  = l.get('mode_saisie', 'unite')
        _base  = _qte_c if _mode == 'colis' else _qte_u
        _prix  = float(l.get('prix_ht', 0))
        _rem   = float(l.get('remise', l.get('remise_pct', 0)) or 0)
        _tva   = tva_def                                     # ← toujours depuis paramètres
        _ht    = round(_base * _prix * (1 - _rem / 100))
        _tvam  = round(_ht * _tva / 100)
        _ttc   = _ht + _tvam
        l['_qte_u'] = _qte_u; l['_qte_c'] = _qte_c
        l['_ht'] = _ht; l['_ttc'] = _ttc
        total_ht  += _ht
        total_tva += _tvam
        total_ttc += _ttc

    doc_id = execute("""INSERT INTO documents_achat(type_doc,reference,fournisseur_id,depot_id,
                        date_doc,date_livraison_prevue,statut,total_ht,total_tva,total_ttc,reste,notes)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     ('commande', ref,
                      f.get('fournisseur_id') or None,
                      resolve_depot_id(f.get('depot_id')),
                      f.get('date_doc', date.today().isoformat()),
                      f.get('date_livraison_prevue'),
                      'en_attente', total_ht, total_tva, total_ttc, total_ttc,
                      f.get('notes')))
    for i, l in enumerate(lignes):
        execute("""INSERT INTO lignes_achat(document_id,article_id,designation,
                   quantite_unite,quantite_colis,prix_achat_ht,tva,total_ht,total_ttc)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (doc_id, l.get('article_id') or l.get('stock_id') or None, l.get('designation',''),
                 l['_qte_u'], l['_qte_c'],
                 float(l.get('prix_ht', 0)),
                 tva_def,
                 l['_ht'], l['_ttc']))

    flash(f"Commande achat {ref} créée.", "success")
    return redirect(url_for('achats_list'))


@app.route('/achats/delete/<int:id>')
@login_required
def achat_delete(id):
    doc = query("SELECT * FROM documents_achat WHERE id=?", (id,), one=True)
    if doc:
        # Interdire la suppression d'une commande réglée (payée intégralement)
        if (doc['montant_paye'] or 0) > 0 and (doc['reste'] or 0) <= 0:
            flash("Commande réglée : suppression impossible.", "warning")
            return redirect(url_for('achats_list'))
        execute("DELETE FROM lignes_achat          WHERE document_id=?",   (id,))
        execute("UPDATE factures_fournisseurs SET commande_id=NULL WHERE commande_id=?", (id,))
        execute("DELETE FROM documents_achat       WHERE id=?",            (id,))
    flash("Commande achat supprimée.", "success")
    return redirect(url_for('achats_list'))


@app.route('/achats/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def achat_edit(id):
    """Modifier une commande achat (uniquement si statut = en_attente)."""
    doc = query("""SELECT da.*, f.nom as fourn_nom
                   FROM documents_achat da
                   LEFT JOIN fournisseurs f ON f.id = da.fournisseur_id
                   WHERE da.id = ? AND da.type_doc = 'commande'""", (id,), one=True)
    if not doc:
        flash("Commande achat introuvable.", "danger")
        return redirect(url_for('achats_list'))
    if doc['statut'] in ('facturee', 'converti'):
        flash("Impossible de modifier une commande déjà facturée.", "warning")
        return redirect(url_for('achats_list'))

    if request.method == 'POST':
        f      = request.form
        lignes = json.loads(f.get('lignes_json', '[]'))
        tva_def = float(get_cfg().get('tva_collectee_taux',
                        get_cfg().get('tva', 0)) or 0)

        total_ht = total_tva = total_ttc = 0
        for l in lignes:
            _col   = max(1, int(l.get('colisage', 1) or 1))
            _qte_u = float(l.get('qte_unite', l.get('quantite', 0)) or 0)
            _qte_c = float(l.get('qte_colis', l.get('colis', 0)) or 0)
            if _qte_c > 0 and _qte_u == 0:
                _qte_u = _qte_c * _col
            elif _qte_u > 0 and _qte_c == 0 and _col > 1:
                _qte_c = int(_qte_u // _col)
            _mode = l.get('mode_saisie', 'unite')
            _base = _qte_c if _mode == 'colis' else _qte_u
            _prix = float(l.get('prix_ht', l.get('prix_achat_ht', 0)) or 0)
            _rem  = float(l.get('remise_pct', l.get('remise', 0)) or 0)
            _tva  = float(l.get('tva_ligne', tva_def) or tva_def)
            _ht   = round(_base * _prix * (1 - _rem / 100))
            _tvam = round(_ht * _tva / 100)
            _ttc  = _ht + _tvam
            l['_qte_u'] = _qte_u; l['_qte_c'] = _qte_c
            l['_ht']    = _ht;    l['_ttc']   = _ttc
            l['_prix']  = _prix;  l['_tva']   = _tva
            total_ht  += _ht
            total_tva += _tvam
            total_ttc += _ttc

        execute("""UPDATE documents_achat
                   SET fournisseur_id=?, depot_id=?, date_doc=?, date_livraison_prevue=?,
                       total_ht=?, total_tva=?, total_ttc=?, reste=?, notes=?
                   WHERE id=?""",
                (f.get('fournisseur_id') or None,
                 f.get('depot_id') or resolve_depot_id(doc['depot_id']),
                 f.get('date_doc', date.today().isoformat()),
                 f.get('date_livraison_prevue') or None,
                 round(total_ht, 2), round(total_tva, 2), round(total_ttc, 2),
                 round(total_ttc, 2),
                 f.get('notes', ''),
                 id))

        execute("DELETE FROM lignes_achat WHERE document_id=?", (id,))
        for l in lignes:
            execute("""INSERT INTO lignes_achat
                (document_id, article_id, designation,
                 quantite_unite, quantite_colis, prix_achat_ht, tva, total_ht, total_ttc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (id, l.get('article_id') or l.get('stock_id') or None, l.get('designation', ''),
                 l['_qte_u'], l['_qte_c'],
                 l['_prix'], l['_tva'],
                 l['_ht'], l['_ttc']))

        flash(f"Commande achat {doc['reference']} mise à jour.", "success")
        return redirect(url_for('achats_list'))

    # GET — pré-remplissage du formulaire avec les lignes existantes
    lignes_db        = query("SELECT * FROM lignes_achat WHERE document_id=? ORDER BY id", (id,))
    fournisseurs_all = query("SELECT id, nom, telephone FROM fournisseurs WHERE actif=1 ORDER BY nom")
    depots_all       = query("SELECT id, code, nom FROM depots WHERE actif=1")
    articles_all     = query("""SELECT a.id, a.reference, a.designation, a.prix_achat_ht,
                                       a.tva, a.colisage, a.unite_vente,
                                       COALESCE(SUM(s.quantite_unite),0) as stock_total
                                FROM articles a
                                LEFT JOIN stocks s ON s.article_id=a.id
                                WHERE a.actif=1
                                GROUP BY a.id ORDER BY a.designation""")
    cfg             = get_cfg()
    edit_doc_d      = dict(doc)
    edit_lignes_d   = [dict(l) for l in lignes_db]
    return render_template('commandes_achats.html',
        cfg=cfg, docs=[], q='', statut_f='',
        fournisseurs_all=fournisseurs_all, depots_all=depots_all,
        articles_all=articles_all,
        today=date.today().isoformat(),
        ca_total=0, ca_mois=0, nb_total=0, nb_attente=0, nb_recues=0,
        nb_devis=0, montant_devis=0, nb_devis_actifs=0,
        edit_doc=edit_doc_d, edit_lignes=edit_lignes_d)


# ══════════════════════════════════════════════════════════════════════
#  RÈGLEMENTS
# ══════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════
#  DEVIS ACHATS
# ══════════════════════════════════════════════════════════════════════

@app.route('/reglements')
@login_required
def reglements_list():
    cfg = get_cfg()
    q = request.args.get('q','')
    mode_f = request.args.get('mode','')
    type_rgl_f = request.args.get('type_rgl','')
    date_du = request.args.get('date_du','')
    date_au = request.args.get('date_au','')
    today = date.today()
    mois_debut = today.replace(day=1).isoformat()

    sql = """SELECT r.*, c.nom as client_nom, c.telephone as client_tel,
                     dv.reference as facture_ref,
                     f.nom as fourn_nom,
                     da.reference as achat_ref
             FROM reglements r
             LEFT JOIN clients c ON c.id=r.client_id
             LEFT JOIN documents_vente dv ON dv.id=r.source_id AND r.source_type IN ('facture','vente')
             LEFT JOIN fournisseurs f ON f.id=r.fournisseur_id
             LEFT JOIN documents_achat da ON da.id=r.source_id AND r.source_type='achat'
             WHERE 1=1"""
    args = []
    if q:
        sql += " AND (r.reference LIKE ? OR c.nom LIKE ? OR f.nom LIKE ?)"
        args += [f'%{q}%', f'%{q}%', f'%{q}%']

    if mode_f:
        sql += " AND r.mode_paiement=?"
        args.append(mode_f)
    if date_du:
        sql += " AND r.date_reglement >= ?"
        args.append(date_du)
    if date_au:
        sql += " AND r.date_reglement <= ?"
        args.append(date_au)
    sql += " ORDER BY r.date_creation DESC"
    reglements = query(sql, args)

    total_jour   = query("SELECT COALESCE(SUM(montant),0) as s FROM reglements WHERE date_reglement=?",
                         (today.isoformat(),), one=True)['s']
    total_global = query("SELECT COALESCE(SUM(montant),0) as s FROM reglements", one=True)['s']
    total_impaye = query("""SELECT COALESCE(SUM(reste),0) as s FROM documents_vente
                            WHERE type_doc='facture' AND statut IN ('en_attente','partielle')""",
                         one=True)['s']
    factures_dues = query("""SELECT dv.id, dv.reference, dv.reste, dv.client_id,
                                    c.nom as client_nom
                             FROM documents_vente dv LEFT JOIN clients c ON c.id=dv.client_id
                             WHERE dv.type_doc='facture' AND dv.statut IN ('en_attente','partielle')
                             ORDER BY dv.date_doc""")
    nb_total = query("SELECT COUNT(*) as c FROM reglements", one=True)['c']
    clients  = query("SELECT * FROM clients WHERE actif=1 ORDER BY CASE WHEN code='CLI000' THEN 0 ELSE 1 END, nom")
    client_defaut = query("SELECT * FROM clients WHERE code='CLI000' AND actif=1", one=True)
    mode_filtre = mode_f

    return render_template('reglements.html', cfg=cfg,
                           reglements=reglements, q=q, mode_f=mode_f, mode_filtre=mode_filtre,
                           total_jour=total_jour, total_global=total_global,
                           total_impaye=total_impaye, nb_total=nb_total,
                           factures_dues=factures_dues, today=today.isoformat(),
                           clients=clients, client_defaut=client_defaut,
                           date_du=date_du, date_au=date_au)

# ── Impression liste règlements (avec filtres) ────────────────────────
@app.route('/reglements/imprimer')
@login_required
def reglements_imprimer():
    cfg = get_cfg()
    _soc = _infos_entreprise()
    q          = request.args.get('q', '')
    mode_f     = request.args.get('mode', '')
    type_rgl_f = request.args.get('type_rgl', '')
    date_du    = request.args.get('date_du', '')
    date_au    = request.args.get('date_au', '')

    sql = """SELECT r.*, c.nom as client_nom,
                     dv.reference as facture_ref
             FROM reglements r
             LEFT JOIN clients c ON c.id=r.client_id
             LEFT JOIN documents_vente dv ON dv.id=r.source_id AND r.source_type IN ('facture','vente')
             WHERE 1=1"""
    args = []
    if q:
        sql += " AND (r.reference LIKE ? OR c.nom LIKE ?)"
        args += [f'%{q}%', f'%{q}%']

    if mode_f:
        sql += " AND r.mode_paiement=?"
        args.append(mode_f)
    if date_du:
        sql += " AND r.date_reglement >= ?"
        args.append(date_du)
    if date_au:
        sql += " AND r.date_reglement <= ?"
        args.append(date_au)
    sql += " ORDER BY r.date_creation DESC"
    reglements = query(sql, args)
    total = sum(float(r['montant'] or 0) for r in reglements)

    modes = {'especes':'Espèces','wave':'Wave','orange_money':'Orange Money',
             'mtn_money':'MTN Money','moov_money':'Moov','carte_bancaire':'Carte',
             'virement':'Virement','cheque':'Chèque'}
    types = {'libre':'Libre','facture':'Facture','vente':'Vente'}

    html = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<title>Règlements — {cfg.get('nom_entreprise','DISTRIGEST') if cfg else 'DISTRIGEST'}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{font-family:'Segoe UI',Arial,sans-serif;font-size:12px;color:#1e293b;padding:20px;}}
  h1{{font-size:18px;font-weight:800;color:#1a3a6c;margin-bottom:2px;}}
  .sub{{font-size:11px;color:#64748b;margin-bottom:16px;}}
  table{{width:100%;border-collapse:collapse;margin-top:12px;}}
  th{{background:#1a3a6c;color:white;padding:7px 10px;font-size:11px;font-weight:700;text-align:left;}}
  td{{padding:6px 10px;border-bottom:1px solid #e2e8f0;font-size:11px;}}
  tr:nth-child(even) td{{background:#f8fafc;}}
  .ttl{{font-weight:800;}}
  .tot{{background:#1a3a6c;color:white;font-weight:800;font-size:12px;}}
  .tot td{{padding:8px 10px;border:none;}}
  @media print{{body{{padding:10px;}}}}
</style></head><body>
<div style="font-size:14px;font-weight:800;color:#1a3a6c;">{_soc['nom']}</div>
<div style="font-size:10.5px;color:#64748b;line-height:1.55;margin:2px 0 8px;">
  {_soc['adresse']}{(' · Tél : ' + _soc['tel']) if _soc['tel'] else ''}{(' · ' + _soc['email']) if _soc['email'] else ''}<br>
  {('NCC : ' + _soc['ncc']) if _soc['ncc'] else ''}{' · ' if _soc['ncc'] and _soc['rccm'] else ''}{('RCCM : ' + _soc['rccm']) if _soc['rccm'] else ''}
</div>
<h1>💳 Liste des Règlements</h1>
<div class="sub">Imprimé le {date.today().strftime('%d/%m/%Y')} — {len(reglements)} règlement(s)</div>
<table>
  <thead><tr><th>Référence</th><th>Date</th><th>Client</th><th>Type</th><th>Mode</th><th>Motif</th><th>Facture</th><th style="text-align:right;">Montant</th></tr></thead>
  <tbody>"""

    for r in reglements:
        src = r['source_type'] or ''
        type_lbl = 'Libre' if not r['source_id'] else 'Règlement'
        date_r = ''
        if r['date_reglement']:
            p = r['date_reglement'].split('-')
            date_r = f"{p[2]}/{p[1]}/{p[0]}" if len(p)==3 else r['date_reglement']
        html += f"""<tr>
      <td class="ttl">{r['reference'] or ''}</td>
      <td>{date_r}</td>
      <td>{r['client_nom'] or '—'}</td>
      <td>{type_lbl}</td>
      <td>{modes.get(r['mode_paiement'], r['mode_paiement'] or '—')}</td>
      <td>{r['motif'] or '—'}</td>
      <td>{r['facture_ref'] or '—'}</td>
      <td style="text-align:right;font-weight:700;">{int(r['montant'] or 0):,} FCFA</td>
    </tr>"""

    html += f"""</tbody>
  <tfoot><tr class="tot"><td colspan="7">TOTAL</td><td style="text-align:right;">{int(total):,} FCFA</td></tr></tfoot>
</table>
<script>window.onload=function(){{window.print();}};</script>
</body></html>"""

    html = _inject_print_format(html, _print_format_css(_current_print_format()))
    return _journal_print_response(html, 'Journal_reglements')


# ── Règlement depuis modal facture/devis/commande vente ──────────────
@app.route('/reglements/ajouter/<int:doc_id>', methods=['POST'])
@login_required
def reglement_ajouter(doc_id):
    """Route appelée depuis le modal Règlement dans devis/commandes/factures."""
    f = request.form
    doc = query("SELECT * FROM documents_vente WHERE id=?", (doc_id,), one=True)
    if not doc:
        flash("Document introuvable.", "danger")
        return redirect(url_for('factures_list'))
    montant = float(f.get('montant_regle') or f.get('montant') or 0)
    if montant <= 0:
        flash("Montant invalide.", "danger")
        return redirect(url_for('factures_list'))
    ref = next_ref_rgl()
    nouveau_paye = (doc['montant_paye'] or 0) + montant
    reste = max(0, (doc['total_ttc'] or 0) - nouveau_paye)
    statut = 'reglee' if reste <= 0 else 'partielle'
    execute("UPDATE documents_vente SET montant_paye=?, reste=?, statut=? WHERE id=?",
            (round(nouveau_paye, 2), round(reste, 2), statut, doc_id))
    date_rgl = (f.get('date_reglement') or '').strip() or date.today().isoformat()
    execute("""INSERT INTO reglements(reference,source_type,source_id,client_id,
               montant,mode_paiement,date_reglement)
               VALUES(?,?,?,?,?,?,?)""",
            (ref, 'facture', doc_id, doc['client_id'],
             round(montant, 2), f.get('mode_paiement', 'especes'), date_rgl))
    flash(f"Règlement {ref} enregistré — {montant:,.0f} FCFA.", "success")
    # ── Reçu soldé si facture entièrement réglée ─────────────────────
    if statut == 'reglee' and doc['type_doc'] == 'facture':
        import threading as _thr_rs
        _thr_rs.Thread(
            target=_notif_envoyer_recu_solde, args=(doc_id,), daemon=True).start()
    # Rediriger vers la bonne liste selon le type de document
    type_doc = doc['type_doc']
    if type_doc == 'devis':
        return redirect(url_for('devis_list'))
    elif type_doc == 'commande':
        return redirect(url_for('commandes_vente_list'))
    else:
        return redirect(url_for('factures_list'))


# ── Alias : /ventes/regler/<id> (utilisé depuis achats.html) ─────────
@app.route('/ventes/regler/<int:doc_id>', methods=['POST'])
@login_required
def vente_regler(doc_id):
    """Alias vers reglement_ajouter."""
    return reglement_ajouter(doc_id)


@app.route('/reglements/add', methods=['POST'])
@login_required
def reglement_add():
    f = request.form
    ref = next_ref_rgl()
    montant = float(f.get('montant', 0) or 0)
    if montant <= 0:
        flash("Montant invalide.", "danger")
        return redirect(url_for('reglements_list'))

    src_type = f.get('source_type', 'libre')
    src_id   = int(f.get('source_id', 0) or 0)

    client_id = _safe_fk(f.get('client_id'))

    if src_id and src_type == 'facture':
        doc = query("SELECT * FROM documents_vente WHERE id=?", (src_id,), one=True)
        if doc:
            client_id    = doc['client_id']
            nouveau_paye = round((doc['montant_paye'] or 0) + montant, 2)
            reste        = round(max(0, (doc['total_ttc'] or 0) - nouveau_paye), 2)
            statut       = 'reglee' if reste <= 0 else 'partielle'
            execute("UPDATE documents_vente SET montant_paye=?, reste=?, statut=? WHERE id=?",
                    (nouveau_paye, reste, statut, src_id))
            # ── Reçu soldé si facture entièrement réglée ─────────────
            if statut == 'reglee':
                import threading as _thr_rs2
                _thr_rs2.Thread(
                    target=_notif_envoyer_recu_solde, args=(src_id,), daemon=True).start()

    execute("""INSERT INTO reglements(reference, source_type, source_id, client_id,
               montant, mode_paiement, date_reglement, notes, motif)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (ref,
             src_type,
             src_id if src_id else None,
             client_id,
             round(montant, 2),
             f.get('mode_paiement', 'especes'),
             f.get('date_reglement', date.today().isoformat()),
             f.get('notes', ''),
             f.get('motif', '')))
    flash(f"Règlement {ref} enregistré.", "success")
    return redirect(url_for('reglements_list'))



@app.route('/reglements/delete/<int:id>')
@login_required
def reglement_delete(id):
    rgl = query("SELECT * FROM reglements WHERE id=?", (id,), one=True)
    if not rgl:
        flash("Règlement introuvable.", "warning")
        return redirect(url_for('reglements_list'))

    source_type = rgl['source_type']
    source_id   = rgl['source_id']
    montant     = float(rgl['montant'] or 0)

    # ── Supprimer le règlement (même s'il est lié à une facture) ────
    # La facture liée est recalculée plus bas (montant_payé / reste / statut).
    execute("DELETE FROM reglements WHERE id=?", (id,))

    # ── Recalculer montant_paye / reste / statut sur la facture client ──
    if source_type in ('facture', 'vente') and source_id:
        doc = query("SELECT * FROM documents_vente WHERE id=?", (source_id,), one=True)
        if doc:
            # Recalculer depuis la somme réelle des règlements restants
            total_paye = query("""SELECT COALESCE(SUM(montant),0) as s FROM reglements
                                  WHERE source_id=? AND source_type IN ('facture','vente')""",
                               (source_id,), one=True)['s']
            total_ttc  = float(doc['total_ttc'] or 0)
            reste      = round(max(0, total_ttc - total_paye), 2)
            if total_paye <= 0:
                statut = 'en_attente'
            elif reste <= 0:
                statut = 'reglee'
            else:
                statut = 'partielle'
            execute("UPDATE documents_vente SET montant_paye=?, reste=?, statut=? WHERE id=?",
                    (round(total_paye, 2), reste, statut, source_id))

    # ── Recalculer sur la facture fournisseur ───────────────────────
    elif source_type == 'facture_fourn' and source_id:
        ff = query("SELECT * FROM factures_fournisseurs WHERE id=?", (source_id,), one=True)
        if ff:
            total_paye = query("""SELECT COALESCE(SUM(montant),0) as s FROM reglements
                                  WHERE source_id=? AND source_type='facture_fourn'""",
                               (source_id,), one=True)['s']
            total_ttc  = float(ff['total_ttc'] or 0)
            reste      = round(max(0, total_ttc - total_paye), 2)
            if total_paye <= 0:
                statut = 'en_attente'
            elif reste <= 0:
                statut = 'reglee'
            else:
                statut = 'partielle'
            execute("UPDATE factures_fournisseurs SET montant_paye=?, reste=?, statut=? WHERE id=?",
                    (round(total_paye, 2), reste, statut, source_id))

    # ── Supprimer l'écriture comptable liée si elle existe ──────────
    execute("DELETE FROM ecritures_comptables WHERE source='reglement' AND source_id=?", (id,))

    flash("Règlement supprimé. La facture a été mise à jour.", "success")
    return redirect(url_for('reglements_list'))

# ══════════════════════════════════════════════════════════════════════
#  DÉPENSES
# ══════════════════════════════════════════════════════════════════════
@app.route('/depenses')
@login_required
def depenses_list():
    cfg = get_cfg()
    today = date.today()
    mois_debut = today.replace(day=1).isoformat()
    depenses = query("SELECT * FROM depenses ORDER BY date_depense DESC")
    total_mois = query("SELECT COALESCE(SUM(montant),0) as s FROM depenses WHERE date_depense>=?",
                       (mois_debut,), one=True)['s']
    par_cat = query("""SELECT categorie, SUM(montant) as total FROM depenses
                       GROUP BY categorie ORDER BY total DESC""")
    return render_template('depenses.html', cfg=cfg, depenses=depenses,
                           total_mois=total_mois, par_cat=par_cat)

@app.route('/depenses/add', methods=['POST'])
@login_required
def depense_add():
    f = request.form
    execute("INSERT INTO depenses(categorie,description,montant,date_depense,responsable,notes) VALUES(?,?,?,?,?,?)",
            (f['categorie'], f['description'], float(f['montant']),
             f.get('date_depense', date.today().isoformat()),
             f.get('responsable'), f.get('notes')))
    flash("Dépense ajoutée.", "success")
    return redirect(url_for('depenses_list'))

@app.route('/depenses/delete/<int:id>')
@login_required
def depense_delete(id):
    execute("DELETE FROM depenses WHERE id=?", (id,))
    flash("Dépense supprimée.", "success")
    return redirect(url_for('depenses_list'))

# ══════════════════════════════════════════════════════════════════════
#  EMPLOYÉS
# ══════════════════════════════════════════════════════════════════════
@app.route('/employes')
@login_required
def employes_list():
    cfg = get_cfg()
    employes = query("SELECT * FROM employes ORDER BY nom")
    return render_template('employes.html', cfg=cfg, employes=employes)

@app.route('/employes/add', methods=['POST'])
@login_required
def employe_add():
    f = request.form
    n = query("SELECT COUNT(*) as c FROM employes", one=True)['c']
    mat = f"EMP{n+1:03d}"
    execute("""INSERT INTO employes(matricule,nom,prenom,poste,telephone,email,
               salaire_base,date_embauche,statut,notes) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (mat, f['nom'].upper(), f.get('prenom'), f.get('poste','Gérant'),
             f.get('telephone'), f.get('email'),
             float(f.get('salaire_base',0) or 0),
             f.get('date_embauche'), f.get('statut','actif'), f.get('notes')))
    flash("Employé ajouté.", "success")
    return redirect(url_for('employes_list'))

@app.route('/employes/edit/<int:id>', methods=['POST'])
@login_required
def employe_edit(id):
    f = request.form
    execute("""UPDATE employes SET nom=?,prenom=?,poste=?,telephone=?,email=?,
               salaire_base=?,date_embauche=?,statut=?,notes=? WHERE id=?""",
            (f['nom'].upper(), f.get('prenom'), f.get('poste'),
             f.get('telephone'), f.get('email'),
             float(f.get('salaire_base',0) or 0),
             f.get('date_embauche'), f.get('statut','actif'),
             f.get('notes'), id))
    flash("Employé modifié.", "success")
    return redirect(url_for('employes_list'))

@app.route('/employes/delete/<int:id>')
@login_required
def employe_delete(id):
    execute("DELETE FROM employes WHERE id=?", (id,))
    flash("Employé supprimé.", "success")
    return redirect(url_for('employes_list'))


# ══════════════════════════════════════════════════════════════════════
#  MODULE PAIE
# ══════════════════════════════════════════════════════════════════════

@app.route('/paie')
@login_required
def paie_list():
    cfg = get_cfg()
    mois_f = request.args.get('mois', '')
    statut_f = request.args.get('statut', '')
    sql = """SELECT fp.*, e.nom, e.prenom, e.matricule, e.poste
             FROM fiches_paie fp JOIN employes e ON e.id=fp.employe_id
             WHERE 1=1"""
    args = []
    if mois_f:   sql += " AND fp.mois=?";     args.append(mois_f)
    if statut_f: sql += " AND fp.statut=?";   args.append(statut_f)
    sql += " ORDER BY fp.mois DESC, e.nom"
    fiches = query(sql, args)
    employes = query("SELECT id, matricule, nom, prenom, poste, salaire_base FROM employes WHERE statut='actif' ORDER BY nom")
    kpi = query("""SELECT
        COUNT(*) as nb_total,
        COALESCE(SUM(salaire_net),0) as total_net,
        COALESCE(SUM(CASE WHEN statut='paye' THEN salaire_net ELSE 0 END),0) as total_paye,
        COUNT(CASE WHEN statut='brouillon' THEN 1 END) as nb_brouillon,
        COUNT(CASE WHEN statut='valide' THEN 1 END) as nb_valide,
        COUNT(CASE WHEN statut='paye' THEN 1 END) as nb_paye
        FROM fiches_paie""", one=True)
    mois_dispo = query("SELECT DISTINCT mois FROM fiches_paie ORDER BY mois DESC")
    return render_template('paie.html', cfg=cfg, fiches=fiches,
                           employes=employes, kpi=kpi,
                           mois_f=mois_f, statut_f=statut_f,
                           mois_dispo=mois_dispo,
                           today=date.today().isoformat(),
                           mois_courant=date.today().strftime('%Y-%m'))


@app.route('/paie/add', methods=['POST'])
@login_required
def paie_add():
    f = request.form
    emp_id = f.get('employe_id')
    emp = query("SELECT * FROM employes WHERE id=?", (emp_id,), one=True)
    if not emp:
        flash("Employé introuvable.", "danger")
        return redirect(url_for('paie_list'))
    sal_base  = float(f.get('salaire_base') or emp['salaire_base'] or 0)
    p_transp  = float(f.get('prime_transport', 0) or 0)
    p_ancien  = float(f.get('prime_anciennete', 0) or 0)
    p_autres  = float(f.get('autres_primes', 0) or 0)
    r_absence = float(f.get('retenue_absence', 0) or 0)
    r_autres  = float(f.get('autres_retenues', 0) or 0)
    brut      = sal_base + p_transp + p_ancien + p_autres
    net       = max(0, brut - r_absence - r_autres)
    mois      = f.get('mois', date.today().strftime('%Y-%m'))
    # Vérifier doublon
    exist = query("SELECT id FROM fiches_paie WHERE employe_id=? AND mois=?", (emp_id, mois), one=True)
    if exist:
        flash(f"Une fiche de paie existe déjà pour {emp['nom']} en {mois}.", "warning")
        return redirect(url_for('paie_list'))
    execute("""INSERT INTO fiches_paie(employe_id,mois,salaire_base,prime_transport,
               prime_anciennete,autres_primes,retenue_absence,autres_retenues,
               salaire_brut,salaire_net,statut,mode_paiement,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (emp_id, mois, sal_base, p_transp, p_ancien, p_autres,
             r_absence, r_autres, round(brut,2), round(net,2),
             'brouillon', f.get('mode_paiement','especes'), f.get('notes','')))
    flash(f"Fiche de paie créée pour {emp['nom']} — {mois}.", "success")
    return redirect(url_for('paie_list'))


@app.route('/paie/<int:id>/valider', methods=['POST'])
@login_required
def paie_valider(id):
    execute("UPDATE fiches_paie SET statut='valide' WHERE id=?", (id,))
    flash("Fiche validée.", "success")
    return redirect(url_for('paie_list'))


@app.route('/paie/<int:id>/payer', methods=['POST'])
@login_required
def paie_payer(id):
    fiche = query("""SELECT fp.*, e.nom, e.prenom
                     FROM fiches_paie fp JOIN employes e ON e.id=fp.employe_id
                     WHERE fp.id=?""", (id,), one=True)
    if not fiche:
        flash("Fiche introuvable.", "danger")
        return redirect(url_for('paie_list'))
    today = date.today().isoformat()
    execute("UPDATE fiches_paie SET statut='paye', date_paiement=? WHERE id=?", (today, id))
    nom_emp = f"{fiche['nom']} {fiche['prenom'] or ''}".strip()
    flash(f"Paie de {nom_emp} marquée comme payée. Cliquez sur « Comptabiliser » pour l'importer en comptabilité.", "success")
    return redirect(url_for('paie_list'))


@app.route('/paie/<int:id>/delete')
@login_required
def paie_delete(id):
    # Supprimer l'écriture comptable liée si elle existe
    execute("DELETE FROM ecritures_comptables WHERE source='paie' AND source_id=?", (id,))
    execute("DELETE FROM fiches_paie WHERE id=?", (id,))
    flash("Fiche de paie supprimée.", "success")
    return redirect(url_for('paie_list'))


@app.route('/paie/<int:id>/print')
@login_required
def paie_print(id):
    fiche = query("""SELECT fp.*, e.nom, e.prenom, e.matricule, e.poste, e.date_embauche
                     FROM fiches_paie fp JOIN employes e ON e.id=fp.employe_id
                     WHERE fp.id=?""", (id,), one=True)
    if not fiche:
        flash("Fiche introuvable.", "danger")
        return redirect(url_for('paie_list'))
    cfg     = get_cfg()
    nom_soc = cfg.get('nom_depot', 'DISTRIGEST')
    adr_soc = cfg.get('adresse', '')
    tel_soc = cfg.get('telephone', '')
    _soc    = _infos_entreprise()
    devise  = cfg.get('devise', 'FCFA')

    def fcfa(v):
        try: return f"{int(float(v or 0)):,}".replace(',', ' ')
        except: return '0'

    nom_emp  = f"{fiche['nom']} {fiche['prenom'] or ''}".strip()
    modes    = {'especes':'Espèces','virement':'Virement','wave':'Wave',
                'orange_money':'Orange Money','mtn_money':'MTN Money',
                'cheque':'Chèque','moov_money':'Moov Money'}
    mode_lbl = modes.get(fiche['mode_paiement'] or '', fiche['mode_paiement'] or '—')
    brut     = fiche['salaire_brut'] or 0
    net      = fiche['salaire_net'] or 0
    retenues = (fiche['retenue_absence'] or 0) + (fiche['autres_retenues'] or 0)

    notes_html = (f"<div style='padding:10px 14px;background:#fefce8;border:1px solid #fde68a;"
                  f"border-radius:8px;font-size:11px;color:#92400e;margin-bottom:16px;'>"
                  f"<strong>Notes :</strong> {fiche['notes']}</div>") if fiche['notes'] else ''

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>Bulletin de Paie — {nom_emp} — {fiche['mois']}</title>
<style>
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0;}}
  body{{font-family:'Segoe UI',Arial,sans-serif;font-size:12px;color:#0f172a;background:white;padding:32px;max-width:680px;margin:0 auto;}}
  .print-btn{{position:fixed;top:20px;right:20px;padding:10px 20px;background:#1a3a6c;color:white;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;box-shadow:0 4px 14px rgba(26,58,108,.3);}}
  .print-btn:hover{{background:#2563eb;}}
  @media print{{.print-btn{{display:none;}}body{{padding:16px;}}}}
  .header{{display:flex;justify-content:space-between;align-items:center;padding-bottom:18px;margin-bottom:18px;border-bottom:3px solid #1a3a6c;}}
  .soc-name{{font-size:18px;font-weight:800;color:#1a3a6c;}}
  .soc-sub{{font-size:11px;color:#64748b;margin-top:3px;}}
  .doc-title{{text-align:right;}}
  .doc-title h1{{font-size:20px;font-weight:900;color:#1a3a6c;text-transform:uppercase;letter-spacing:1px;}}
  .doc-title .mois{{font-size:13px;font-weight:700;color:#2563eb;margin-top:4px;}}
  .doc-title .ref{{font-size:10px;color:#94a3b8;margin-top:2px;}}
  .emp-bloc{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:18px;}}
  .emp-card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px;}}
  .emp-card h3{{font-size:9px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px;}}
  .emp-card .val{{font-size:13px;font-weight:700;color:#0f172a;margin-bottom:3px;}}
  .emp-card .sub{{font-size:10px;color:#64748b;margin-bottom:2px;}}
  .recap{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:18px;}}
  .recap-item{{text-align:center;padding:12px;border-radius:10px;border:1.5px solid;}}
  .recap-item .lbl{{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;}}
  .recap-item .val{{font-size:16px;font-weight:900;font-variant-numeric:tabular-nums;}}
  .rc-brut{{background:#f0f9ff;border-color:#bae6fd;color:#0369a1;}}
  .rc-ret{{background:#fff5f5;border-color:#fecaca;color:#dc2626;}}
  .rc-net{{background:#f0fdf4;border-color:#bbf7d0;color:#15803d;}}
  table{{width:100%;border-collapse:collapse;margin-bottom:18px;}}
  thead tr{{background:#1a3a6c;}}
  thead th{{padding:9px 14px;color:white;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;text-align:left;}}
  thead th.r{{text-align:right;}}
  tbody td{{padding:9px 14px;border-bottom:1px solid #f1f5f9;font-size:12px;}}
  tbody td.r{{text-align:right;font-variant-numeric:tabular-nums;}}
  tr.section td{{font-size:10px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.5px;padding:6px 14px;background:#f8fafc;}}
  tfoot tr{{background:#0f172a;}}
  tfoot td{{padding:12px 14px;color:white;font-weight:800;font-size:14px;}}
  tfoot td.r{{text-align:right;font-size:16px;color:#4ade80;}}
  .sigs{{display:flex;justify-content:space-between;margin-top:36px;}}
  .signature{{border-top:1px solid #cbd5e1;width:160px;text-align:center;padding-top:6px;font-size:10px;color:#64748b;}}
  .footer{{margin-top:24px;padding-top:12px;border-top:1px solid #e2e8f0;display:flex;justify-content:space-between;font-size:10px;color:#94a3b8;}}
  .badge-paye{{display:inline-flex;align-items:center;gap:5px;padding:4px 12px;background:#dcfce7;color:#15803d;border-radius:20px;font-size:11px;font-weight:700;border:1px solid #bbf7d0;}}
</style>
</head>
<body>
<button class="print-btn" onclick="window.print()">🖨️ Imprimer / PDF</button>

<div class="header">
  <div>
    <div class="soc-name">🛒 {_soc['nom']}</div>
    <div class="soc-sub">{_soc['adresse']}</div>
    <div class="soc-sub">{(_soc['tel'] + ('  ·  ' if _soc['tel'] and _soc['email'] else '') + _soc['email'])}</div>
    <div class="soc-sub">{('NCC : ' + _soc['ncc'] if _soc['ncc'] else '') + (' · ' if _soc['ncc'] and _soc['rccm'] else '') + ('RCCM : ' + _soc['rccm'] if _soc['rccm'] else '')}</div>
  </div>
  <div class="doc-title">
    <h1>Bulletin de Paie</h1>
    <div class="mois">Période : {fiche['mois']}</div>
    <div class="ref">Réf. PAY-{fiche['id']:05d}</div>
    <div style="margin-top:8px;"><span class="badge-paye">✅ Payée le {fiche['date_paiement'] or '—'}</span></div>
  </div>
</div>

<div class="emp-bloc">
  <div class="emp-card">
    <h3>👤 Employé</h3>
    <div class="val">{nom_emp}</div>
    <div class="sub">Matricule : {fiche['matricule'] or '—'}</div>
    <div class="sub">Poste : {fiche['poste'] or '—'}</div>
    <div class="sub">Date embauche : {fiche['date_embauche'] or '—'}</div>
  </div>
  <div class="emp-card">
    <h3>🏢 Employeur</h3>
    <div class="val">{nom_soc}</div>
    <div class="sub">{adr_soc}</div>
    <div class="sub">Mode paiement : {mode_lbl}</div>
    <div class="sub">Date paiement : {fiche['date_paiement'] or '—'}</div>
  </div>
</div>

<div class="recap">
  <div class="recap-item rc-brut">
    <div class="lbl">Salaire Brut</div>
    <div class="val">{fcfa(brut)} {devise}</div>
  </div>
  <div class="recap-item rc-ret">
    <div class="lbl">Retenues</div>
    <div class="val">−{fcfa(retenues)} {devise}</div>
  </div>
  <div class="recap-item rc-net">
    <div class="lbl">Net à Payer</div>
    <div class="val">{fcfa(net)} {devise}</div>
  </div>
</div>

<table>
  <thead><tr><th>Élément de paie</th><th class="r">Montant ({devise})</th></tr></thead>
  <tbody>
    <tr class="section"><td colspan="2">▶ ÉLÉMENTS DE RÉMUNÉRATION</td></tr>
    <tr><td>Salaire de base</td><td class="r">{fcfa(fiche['salaire_base'] or 0)}</td></tr>
    <tr><td>Prime de transport</td><td class="r">{fcfa(fiche['prime_transport'] or 0)}</td></tr>
    <tr><td>Prime d'ancienneté</td><td class="r">{fcfa(fiche['prime_anciennete'] or 0)}</td></tr>
    <tr><td>Autres primes</td><td class="r">{fcfa(fiche['autres_primes'] or 0)}</td></tr>
    <tr style="font-weight:700;background:#dbeafe;"><td>Salaire Brut</td><td class="r" style="color:#1d4ed8;">{fcfa(brut)}</td></tr>
    <tr class="section"><td colspan="2">▶ RETENUES</td></tr>
    <tr><td>Retenue absence</td><td class="r" style="color:#dc2626;">−{fcfa(fiche['retenue_absence'] or 0)}</td></tr>
    <tr><td>Autres retenues</td><td class="r" style="color:#dc2626;">−{fcfa(fiche['autres_retenues'] or 0)}</td></tr>
    <tr style="font-weight:700;background:#fee2e2;"><td>Total Retenues</td><td class="r" style="color:#dc2626;">−{fcfa(retenues)}</td></tr>
  </tbody>
  <tfoot><tr><td>💰 NET À PAYER</td><td class="r">{fcfa(net)} {devise}</td></tr></tfoot>
</table>

{notes_html}

<div class="sigs">
  <div class="signature">Signature Employé</div>
  <div class="signature">Cachet &amp; Signature Employeur</div>
</div>

<div class="footer">
  <span>{nom_soc} · Bulletin de Paie · {fiche['mois']} · PAY-{fiche['id']:05d}</span>
  <span>DISTRIGEST · STiNAUG TECHNOLOGIE</span>
</div>
<script>window.addEventListener('load',function(){{setTimeout(function(){{window.print();}},250);}});</script>
</body>
</html>"""
    html = _inject_print_format(html, _print_format_css(_current_print_format()))
    html = _journal_print_response(html, 'document_imprime')
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}



@app.route('/conges')
@login_required
def conges_list():
    cfg = get_cfg()
    statut_f = request.args.get('statut', '')
    sql = """SELECT cg.*, e.nom, e.prenom, e.matricule, e.poste
             FROM conges cg JOIN employes e ON e.id=cg.employe_id
             WHERE 1=1"""
    args = []
    if statut_f: sql += " AND cg.statut=?"; args.append(statut_f)
    sql += " ORDER BY cg.date_debut DESC"
    conges = query(sql, args)
    employes = query("SELECT id, matricule, nom, prenom, poste FROM employes WHERE statut='actif' ORDER BY nom")
    kpi = query("""SELECT
        COUNT(*) as nb_total,
        COALESCE(SUM(nb_jours),0) as total_jours,
        COUNT(CASE WHEN statut='en_attente' THEN 1 END) as nb_attente,
        COUNT(CASE WHEN statut='approuve' THEN 1 END) as nb_approuve,
        COUNT(CASE WHEN statut='refuse' THEN 1 END) as nb_refuse
        FROM conges""", one=True)
    return render_template('conges.html', cfg=cfg, conges=conges,
                           employes=employes, kpi=kpi,
                           statut_f=statut_f,
                           today=date.today().isoformat())


@app.route('/conges/add', methods=['POST'])
@login_required
def conge_add():
    f = request.form
    d_debut = f.get('date_debut')
    d_fin   = f.get('date_fin')
    # Calculer nb jours
    try:
        from datetime import datetime as dt
        nb_j = (dt.fromisoformat(d_fin) - dt.fromisoformat(d_debut)).days + 1
        nb_j = max(1, nb_j)
    except Exception:
        nb_j = int(f.get('nb_jours', 1) or 1)
    execute("""INSERT INTO conges(employe_id,type_conge,date_debut,date_fin,nb_jours,statut,motif,notes)
               VALUES(?,?,?,?,?,?,?,?)""",
            (f['employe_id'], f.get('type_conge','annuel'),
             d_debut, d_fin, nb_j, 'en_attente',
             f.get('motif',''), f.get('notes','')))
    flash("Demande de congé enregistrée.", "success")
    return redirect(url_for('conges_list'))


@app.route('/conges/<int:id>/statut', methods=['POST'])
@login_required
def conge_statut(id):
    statut = request.form.get('statut')
    if statut in ('approuve','refuse','termine'):
        execute("UPDATE conges SET statut=? WHERE id=?", (statut, id))
        flash(f"Congé mis à jour : {statut}.", "success")
    return redirect(url_for('conges_list'))


@app.route('/conges/<int:id>/delete')
@login_required
def conge_delete(id):
    execute("DELETE FROM conges WHERE id=?", (id,))
    flash("Congé supprimé.", "success")
    return redirect(url_for('conges_list'))


# ══════════════════════════════════════════════════════════════════════
#  MODULE NOTIFICATIONS AUTOMATIQUES — Gmail · WhatsApp · SMS
# ══════════════════════════════════════════════════════════════════════

def _notif_fmt_fcfa(v):
    try:
        return f"{int(float(v or 0)):,}".replace(',', ' ')
    except Exception:
        return '0'


def _notif_build_msg(tpl, client_nom, montant_du, nom_depot):
    return (str(tpl)
            .replace('{client}',   str(client_nom or ''))
            .replace('{montant}',  _notif_fmt_fcfa(montant_du))
            .replace('{societe}',  str(nom_depot or 'DISTRIGEST'))
            .replace('{date}',     date.today().strftime('%d/%m/%Y')))


def _notif_send_gmail(cfg_n, nom_depot, client_email, client_nom, montant_du,
                      sujet_tpl, corps_tpl):
    """Envoi email via compte Gmail avec App Password (SMTP TLS port 587)."""
    gmail_user = (cfg_n.get('notif_gmail_user') or '').strip()
    gmail_pwd  = (cfg_n.get('notif_gmail_pwd')  or '').strip()
    if not gmail_user or not gmail_pwd:
        return False, "Identifiants Gmail non configurés"
    if not client_email:
        return False, "Client sans email"

    sujet = _notif_build_msg(sujet_tpl, client_nom, montant_du, nom_depot)
    corps = _notif_build_msg(corps_tpl, client_nom, montant_du, nom_depot)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = sujet
    msg['From']    = gmail_user
    msg['To']      = client_email

    html_body = """<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#1e293b;">
<div style="max-width:520px;margin:auto;padding:28px 24px;border:1px solid #e2e8f0;border-radius:12px;">
  <div style="background:linear-gradient(135deg,#1e3a8a,#2563eb);padding:16px 20px;
              border-radius:8px;margin-bottom:20px;">
    <span style="color:white;font-size:18px;font-weight:700;">🍺 {societe}</span>
  </div>
  <p style="margin:0 0 16px;">{corps_html}</p>
  <div style="background:#fff1f2;border:1px solid #fecdd3;border-radius:8px;
              padding:14px 18px;margin:18px 0;">
    <div style="font-size:12px;color:#64748b;">Montant impayé</div>
    <div style="font-size:22px;font-weight:800;color:#dc2626;">{montant} FCFA</div>
  </div>
  <p style="font-size:11px;color:#94a3b8;margin-top:20px;border-top:1px solid #e2e8f0;
            padding-top:12px;">
    Message envoyé automatiquement par DISTRIGEST. Ne pas répondre directement.
  </p>
</div></body></html>""".replace('{societe}', str(nom_depot or 'DISTRIGEST')) \
                      .replace('{corps_html}', corps.replace('\n', '<br>')) \
                      .replace('{montant}', _notif_fmt_fcfa(montant_du))

    msg.attach(MIMEText(corps,    'plain', 'utf-8'))
    msg.attach(MIMEText(html_body,'html',  'utf-8'))
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=20) as srv:
            srv.ehlo()
            srv.starttls(context=ctx)
            srv.login(gmail_user, gmail_pwd)
            srv.sendmail(gmail_user, client_email, msg.as_string())
        return True, None
    except Exception as exc:
        return False, str(exc)


# Version de l'API Graph de Meta (WhatsApp Cloud API). À incrémenter si besoin.
_WA_GRAPH_VERSION = 'v23.0'


def _notif_fmt_tel_intl(client_tel, indicatif_defaut='225'):
    """Normalise un numéro en format international SANS le '+', ex : 2250700000000.
    Si aucun indicatif n'est présent, applique celui de la Côte d'Ivoire (225)."""
    tel = (client_tel or '').strip()
    for ch in (' ', '-', '.', '(', ')'):
        tel = tel.replace(ch, '')
    if tel.startswith('+'):
        tel = tel[1:]
    elif tel.startswith('00'):
        tel = tel[2:]
    elif tel.startswith('0'):
        tel = indicatif_defaut + tel.lstrip('0')
    elif not tel.startswith(indicatif_defaut):
        tel = indicatif_defaut + tel
    return tel


def _notif_send_whatsapp(cfg_n, client_tel, client_nom, montant_du,
                         corps_tpl, nom_depot):
    """Envoi WhatsApp via l'API Cloud officielle de Meta (graph.facebook.com).
    Le texte est défini par un MODÈLE (template) approuvé dans Meta Business
    Manager, avec 2 variables : {{1}} = nom du client, {{2}} = montant.
    corps_tpl n'est pas utilisé ici (le texte vit côté Meta)."""
    import json as _json, urllib.request as _ur, urllib.error as _ue
    phone_id = (cfg_n.get('notif_wa_phone_id') or '').strip()
    token    = (cfg_n.get('notif_wa_token')    or '').strip()
    template = (cfg_n.get('notif_wa_template') or '').strip()
    lang     = (cfg_n.get('notif_wa_lang')     or 'fr').strip()
    if not (phone_id and token and template):
        return False, "WhatsApp non configuré (ID du numéro, token et modèle requis)"
    if not client_tel:
        return False, "Client sans téléphone"
    tel = _notif_fmt_tel_intl(client_tel)
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": tel,
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": lang or 'fr'},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": str(client_nom or 'client')},
                    {"type": "text", "text": _notif_fmt_fcfa(montant_du)},
                ],
            }],
        },
    }
    url = f"https://graph.facebook.com/{_WA_GRAPH_VERSION}/{phone_id}/messages"
    req = _ur.Request(url, data=_json.dumps(payload).encode('utf-8'), method='POST')
    req.add_header('Authorization', 'Bearer ' + token)
    req.add_header('Content-Type', 'application/json')
    try:
        with _ur.urlopen(req, timeout=20) as resp:
            resp.read()
        return True, None
    except _ue.HTTPError as he:
        try:
            err = _json.loads(he.read().decode('utf-8', 'replace'))
            msg = err.get('error', {}).get('message', '') or f"HTTP {he.code}"
        except Exception:
            msg = f"HTTP {he.code}"
        return False, "Meta : " + msg
    except Exception as exc:
        return False, str(exc)


def _notif_send_sms(cfg_n, client_tel, client_nom, montant_du,
                    corps_tpl, nom_depot):
    """Envoi SMS via une passerelle locale (API REST configurable).
    Format câblé : POST JSON {"sender","to","message"} + en-tête
    'Authorization: Bearer <clé>'. Convient aux passerelles acceptant ce schéma.
    Pour Orange CI (OAuth2) ou LeTexto (2 étapes), adapter cette fonction
    au format exact du fournisseur."""
    import json as _json, urllib.request as _ur, urllib.error as _ue
    api_url = (cfg_n.get('notif_sms_api_url') or '').strip()
    api_key = (cfg_n.get('notif_sms_api_key') or '').strip()
    sender  = (cfg_n.get('notif_sms_sender')  or (nom_depot or 'INFO')).strip()
    if not api_url or not api_key:
        return False, "Passerelle SMS non configurée (URL + clé API requises)"
    if not client_tel:
        return False, "Client sans téléphone"
    tel = _notif_fmt_tel_intl(client_tel)
    corps = _notif_build_msg(corps_tpl, client_nom, montant_du, nom_depot)
    if len(corps) > 160:
        corps = corps[:157] + '...'
    payload = {"sender": sender, "to": tel, "message": corps}
    req = _ur.Request(api_url, data=_json.dumps(payload).encode('utf-8'), method='POST')
    req.add_header('Authorization', 'Bearer ' + api_key)
    req.add_header('Content-Type', 'application/json')
    try:
        with _ur.urlopen(req, timeout=20) as resp:
            code = resp.getcode()
        if 200 <= int(code) < 300:
            return True, None
        return False, f"HTTP {code}"
    except _ue.HTTPError as he:
        try:
            body = he.read().decode('utf-8', 'replace')[:160]
        except Exception:
            body = ''
        return False, (f"HTTP {he.code} {body}").strip()
    except Exception as exc:
        return False, str(exc)


def _notif_lancer_cycle():
    """
    Cœur du moteur de relances : lit les paramètres, trouve CHAQUE FACTURE impayée,
    envoie une notification si elle n'en a pas reçu depuis 5 jours, et trace dans
    historique_notifications (colonne facture_id + type_notif='relance').
    Appelé par le scheduler OU par la route 'Envoyer maintenant'.
    Délai fixe : 5 jours entre deux rappels par facture.
    """
    _RELANCE_DELAI_J = 5   # jours entre deux rappels par facture

    _db = sqlite3.connect(DB_PATH)
    _db.row_factory = sqlite3.Row
    try:
        rows = _db.execute(
            "SELECT cle, valeur FROM parametres WHERE cle LIKE 'notif_%' OR cle='nom_depot'"
        ).fetchall()
        cfg_n = {r['cle']: r['valeur'] for r in rows}

        if cfg_n.get('notif_auto_active', 'non') != 'oui':
            _db.close()
            return

        nom_depot      = cfg_n.get('nom_depot', 'DISTRIGEST')
        canal_gmail    = cfg_n.get('notif_canal_gmail',    'non') == 'oui'
        canal_whatsapp = cfg_n.get('notif_canal_whatsapp', 'non') == 'oui'
        canal_sms      = cfg_n.get('notif_canal_sms',      'non') == 'oui'

        if not (canal_gmail or canal_whatsapp or canal_sms):
            _db.close()
            return

        sujet_g  = (cfg_n.get('notif_sujet_gmail') or
                              'Rappel de paiement — {societe}')
        corps_g  = (cfg_n.get('notif_corps_gmail') or
                              'Cher(e) {client},\n\nNous vous rappelons qu\'un montant de '
                              '{montant} FCFA est en attente de règlement.\n\n'
                              'Merci de régulariser dans les meilleurs délais.\n\nCordialement,\n{societe}')
        corps_wa = (cfg_n.get('notif_corps_whatsapp') or
                              '🔔 *{societe}*\nBonjour {client}, votre solde impayé est de '
                              '*{montant} FCFA*.\nMerci de régulariser dès que possible. 🙏')
        corps_sm = (cfg_n.get('notif_corps_sms') or
                              '{societe}: Bonjour {client}, solde impayé {montant} FCFA. '
                              'Merci de nous contacter rapidement.')

        # ── Requête par FACTURE (et non par client global) ──────────────
        factures = _db.execute("""
            SELECT dv.id AS fac_id, dv.reference, dv.reste,
                   c.id AS c_id, c.nom, c.email, c.telephone
            FROM documents_vente dv
            JOIN clients c ON c.id = dv.client_id
            WHERE dv.type_doc = 'facture'
              AND dv.statut IN ('en_attente', 'partielle')
              AND dv.reste > 0
              AND c.actif = 1
            ORDER BY dv.reste DESC
        """).fetchall()

        nb_traites = 0
        for fac in factures:
            fac_id  = fac['fac_id']
            c_id    = fac['c_id']
            c_nom   = fac['nom']
            c_mail  = fac['email']
            c_tel   = fac['telephone']
            montant = fac['reste']

            # Vérifier le délai depuis le dernier rappel pour CETTE facture
            last = _db.execute(
                "SELECT MAX(date_envoi) FROM historique_notifications "
                "WHERE facture_id=? AND type_notif='relance' AND statut='ok'",
                (fac_id,)
            ).fetchone()[0]
            if last:
                try:
                    delta = (date.today() -
                             datetime.fromisoformat(last[:10]).date()).days
                    if delta < _RELANCE_DELAI_J:
                        continue
                except Exception:
                    pass

            if canal_gmail and c_mail:
                ok, err = _notif_send_gmail(
                    cfg_n, nom_depot, c_mail, c_nom, montant, sujet_g, corps_g)
                _db.execute(
                    "INSERT INTO historique_notifications"
                    "(client_id,facture_id,type_notif,canal,statut,montant_du,erreur)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (c_id, fac_id, 'relance', 'gmail',
                     'ok' if ok else 'erreur', montant, err))

            if canal_whatsapp and c_tel:
                ok, err = _notif_send_whatsapp(
                    cfg_n, c_tel, c_nom, montant, corps_wa, nom_depot)
                _db.execute(
                    "INSERT INTO historique_notifications"
                    "(client_id,facture_id,type_notif,canal,statut,montant_du,erreur)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (c_id, fac_id, 'relance', 'whatsapp',
                     'ok' if ok else 'erreur', montant, err))

            if canal_sms and c_tel:
                ok, err = _notif_send_sms(
                    cfg_n, c_tel, c_nom, montant, corps_sm, nom_depot)
                _db.execute(
                    "INSERT INTO historique_notifications"
                    "(client_id,facture_id,type_notif,canal,statut,montant_du,erreur)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (c_id, fac_id, 'relance', 'sms',
                     'ok' if ok else 'erreur', montant, err))

            _db.commit()
            nb_traites += 1

        logging.info("[NOTIF] Cycle relances terminé — %d facture(s) traitée(s)", nb_traites)
    except Exception as _exc:
        logging.error("[NOTIF] Erreur cycle : %s", _exc)
    finally:
        _db.close()


def _notif_envoyer_creation_facture(doc_id):
    """
    Envoie immédiatement une notification au client lors de la création d'une
    facture impayée (reste > 0). Appelé en thread daemon depuis la route de
    création de document.
    """
    _db = sqlite3.connect(DB_PATH)
    _db.row_factory = sqlite3.Row
    try:
        rows = _db.execute(
            "SELECT cle, valeur FROM parametres WHERE cle LIKE 'notif_%' OR cle='nom_depot'"
        ).fetchall()
        cfg_n = {r['cle']: r['valeur'] for r in rows}

        if cfg_n.get('notif_auto_active', 'non') != 'oui':
            return

        nom_depot      = cfg_n.get('nom_depot', 'DISTRIGEST')
        canal_gmail    = cfg_n.get('notif_canal_gmail',    'non') == 'oui'
        canal_whatsapp = cfg_n.get('notif_canal_whatsapp', 'non') == 'oui'
        canal_sms      = cfg_n.get('notif_canal_sms',      'non') == 'oui'

        if not (canal_gmail or canal_whatsapp or canal_sms):
            return

        fac = _db.execute("""
            SELECT dv.id, dv.reference, dv.reste, dv.statut,
                   c.id AS c_id, c.nom, c.email, c.telephone
            FROM documents_vente dv
            JOIN clients c ON c.id = dv.client_id
            WHERE dv.id = ? AND dv.type_doc = 'facture'
        """, (doc_id,)).fetchone()

        if not fac or fac['statut'] == 'reglee' or (fac['reste'] or 0) <= 0:
            return   # facture inexistante ou déjà réglée → pas de notif

        c_id    = fac['c_id']
        c_nom   = fac['nom']
        c_mail  = fac['email']
        c_tel   = fac['telephone']
        montant = fac['reste']
        ref     = fac['reference']

        sujet_g = f"Nouvelle facture {ref} — {{societe}}"
        corps_g = (
            f"Cher(e) {{client}},\n\n"
            f"Votre facture {ref} d'un montant de {{montant}} FCFA vient d'être émise.\n\n"
            f"Merci de procéder au règlement dans les meilleurs délais.\n\n"
            f"Cordialement,\n{{societe}}"
        )
        corps_wa = (
            f"🧾 *{{societe}}*\nBonjour {{client}}, votre facture *{ref}* "
            f"d'un montant de *{{montant}} FCFA* vient d'être créée.\n"
            f"Merci de régler dès que possible. 🙏"
        )
        corps_sm = (
            f"{{societe}}: Facture {ref} émise — {{montant}} FCFA à régler. "
            f"Merci de nous contacter."
        )

        if canal_gmail and c_mail:
            ok, err = _notif_send_gmail(cfg_n, nom_depot, c_mail, c_nom, montant,
                                        sujet_g, corps_g)
            _db.execute(
                "INSERT INTO historique_notifications"
                "(client_id,facture_id,type_notif,canal,statut,montant_du,erreur)"
                " VALUES(?,?,?,?,?,?,?)",
                (c_id, doc_id, 'creation', 'gmail', 'ok' if ok else 'erreur', montant, err))

        if canal_whatsapp and c_tel:
            ok, err = _notif_send_whatsapp(cfg_n, c_tel, c_nom, montant, corps_wa, nom_depot)
            _db.execute(
                "INSERT INTO historique_notifications"
                "(client_id,facture_id,type_notif,canal,statut,montant_du,erreur)"
                " VALUES(?,?,?,?,?,?,?)",
                (c_id, doc_id, 'creation', 'whatsapp', 'ok' if ok else 'erreur', montant, err))

        if canal_sms and c_tel:
            ok, err = _notif_send_sms(cfg_n, c_tel, c_nom, montant, corps_sm, nom_depot)
            _db.execute(
                "INSERT INTO historique_notifications"
                "(client_id,facture_id,type_notif,canal,statut,montant_du,erreur)"
                " VALUES(?,?,?,?,?,?,?)",
                (c_id, doc_id, 'creation', 'sms', 'ok' if ok else 'erreur', montant, err))

        _db.commit()
        logging.info("[NOTIF] Notification création facture %s envoyée au client %s",
                     ref, c_nom)
    except Exception as _exc:
        logging.error("[NOTIF] Erreur notification création facture %s : %s", doc_id, _exc)
    finally:
        _db.close()


def _notif_envoyer_recu_solde(doc_id):
    """
    Envoie un reçu de solde au client dès qu'une facture passe au statut 'reglee'.
    Appelé en thread daemon depuis toute route de règlement.
    N'envoie qu'une seule fois (vérifie l'historique type_notif='recu_solde').
    """
    _db = sqlite3.connect(DB_PATH)
    _db.row_factory = sqlite3.Row
    try:
        rows = _db.execute(
            "SELECT cle, valeur FROM parametres WHERE cle LIKE 'notif_%' OR cle='nom_depot'"
        ).fetchall()
        cfg_n = {r['cle']: r['valeur'] for r in rows}

        if cfg_n.get('notif_auto_active', 'non') != 'oui':
            return

        nom_depot      = cfg_n.get('nom_depot', 'DISTRIGEST')
        canal_gmail    = cfg_n.get('notif_canal_gmail',    'non') == 'oui'
        canal_whatsapp = cfg_n.get('notif_canal_whatsapp', 'non') == 'oui'
        canal_sms      = cfg_n.get('notif_canal_sms',      'non') == 'oui'

        if not (canal_gmail or canal_whatsapp or canal_sms):
            return

        fac = _db.execute("""
            SELECT dv.id, dv.reference, dv.total_ttc, dv.statut,
                   c.id AS c_id, c.nom, c.email, c.telephone
            FROM documents_vente dv
            JOIN clients c ON c.id = dv.client_id
            WHERE dv.id = ? AND dv.type_doc = 'facture'
        """, (doc_id,)).fetchone()

        if not fac or fac['statut'] != 'reglee':
            return   # pas encore réglée

        # Ne pas renvoyer un reçu déjà envoyé pour cette facture
        already = _db.execute(
            "SELECT id FROM historique_notifications "
            "WHERE facture_id=? AND type_notif='recu_solde' AND statut='ok' LIMIT 1",
            (doc_id,)
        ).fetchone()
        if already:
            return

        c_id    = fac['c_id']
        c_nom   = fac['nom']
        c_mail  = fac['email']
        c_tel   = fac['telephone']
        montant = fac['total_ttc']
        ref     = fac['reference']
        today   = date.today().strftime('%d/%m/%Y')

        sujet_g = f"✅ Facture {ref} soldée — {nom_depot}"
        corps_g = (
            f"Cher(e) {c_nom},\n\n"
            f"Nous avons bien reçu votre règlement.\n"
            f"La facture {ref} d'un montant de {_notif_fmt_fcfa(montant)} FCFA "
            f"est désormais entièrement soldée au {today}.\n\n"
            f"Merci pour votre confiance.\n\nCordialement,\n{nom_depot}"
        )
        corps_wa = (
            f"✅ *{nom_depot}*\nBonjour {c_nom}, votre facture *{ref}* "
            f"({_notif_fmt_fcfa(montant)} FCFA) est entièrement soldée. "
            f"Merci pour votre règlement ! 🎉"
        )
        corps_sm = (
            f"{nom_depot}: Facture {ref} soldée — {_notif_fmt_fcfa(montant)} FCFA "
            f"reçus. Merci !"
        )

        # Pour l'email on utilise un template HTML spécial (reçu vert)
        def _send_recu_gmail():
            gmail_user = (cfg_n.get('notif_gmail_user') or '').strip()
            gmail_pwd  = (cfg_n.get('notif_gmail_pwd')  or '').strip()
            if not gmail_user or not gmail_pwd or not c_mail:
                return False, "Non configuré ou client sans email"
            msg = MIMEMultipart('alternative')
            msg['Subject'] = sujet_g
            msg['From']    = gmail_user
            msg['To']      = c_mail
            plain = corps_g
            html_body = f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#1e293b;">
<div style="max-width:520px;margin:auto;padding:28px 24px;border:1px solid #e2e8f0;border-radius:12px;">
  <div style="background:linear-gradient(135deg,#166534,#16a34a);padding:16px 20px;
              border-radius:8px;margin-bottom:20px;">
    <span style="color:white;font-size:18px;font-weight:700;">✅ {nom_depot} — Reçu de règlement</span>
  </div>
  <p>Cher(e) <strong>{c_nom}</strong>,</p>
  <p>Nous avons bien reçu votre règlement. Votre facture est désormais entièrement soldée.</p>
  <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;
              padding:14px 18px;margin:18px 0;">
    <div style="font-size:12px;color:#64748b;">Facture soldée</div>
    <div style="font-size:20px;font-weight:800;color:#166534;">{ref}</div>
    <div style="font-size:14px;color:#15803d;margin-top:6px;">
      Montant total : <strong>{_notif_fmt_fcfa(montant)} FCFA</strong>
    </div>
    <div style="font-size:11px;color:#64748b;margin-top:4px;">Date : {today}</div>
  </div>
  <p>Merci pour votre confiance et votre ponctualité.</p>
  <p style="font-size:11px;color:#94a3b8;margin-top:20px;border-top:1px solid #e2e8f0;
            padding-top:12px;">
    Message envoyé automatiquement par DISTRIGEST. Ne pas répondre directement.
  </p>
</div></body></html>"""
            msg.attach(MIMEText(plain,    'plain', 'utf-8'))
            msg.attach(MIMEText(html_body,'html',  'utf-8'))
            try:
                ctx = ssl.create_default_context()
                with smtplib.SMTP('smtp.gmail.com', 587, timeout=20) as srv:
                    srv.ehlo(); srv.starttls(context=ctx)
                    srv.login(gmail_user, gmail_pwd)
                    srv.sendmail(gmail_user, c_mail, msg.as_string())
                return True, None
            except Exception as exc:
                return False, str(exc)

        if canal_gmail and c_mail:
            ok, err = _send_recu_gmail()
            _db.execute(
                "INSERT INTO historique_notifications"
                "(client_id,facture_id,type_notif,canal,statut,montant_du,erreur)"
                " VALUES(?,?,?,?,?,?,?)",
                (c_id, doc_id, 'recu_solde', 'gmail', 'ok' if ok else 'erreur', montant, err))

        if canal_whatsapp and c_tel:
            ok, err = _notif_send_whatsapp(cfg_n, c_tel, c_nom, montant, corps_wa, nom_depot)
            _db.execute(
                "INSERT INTO historique_notifications"
                "(client_id,facture_id,type_notif,canal,statut,montant_du,erreur)"
                " VALUES(?,?,?,?,?,?,?)",
                (c_id, doc_id, 'recu_solde', 'whatsapp', 'ok' if ok else 'erreur', montant, err))

        if canal_sms and c_tel:
            ok, err = _notif_send_sms(cfg_n, c_tel, c_nom, montant, corps_sm, nom_depot)
            _db.execute(
                "INSERT INTO historique_notifications"
                "(client_id,facture_id,type_notif,canal,statut,montant_du,erreur)"
                " VALUES(?,?,?,?,?,?,?)",
                (c_id, doc_id, 'recu_solde', 'sms', 'ok' if ok else 'erreur', montant, err))

        _db.commit()
        logging.info("[NOTIF] Reçu soldé facture %s envoyé au client %s", ref, c_nom)
    except Exception as _exc:
        logging.error("[NOTIF] Erreur reçu soldé facture %s : %s", doc_id, _exc)
    finally:
        _db.close()


def _notif_demarrer_scheduler():
    """
    Démarre un thread daemon qui tourne chaque minute et déclenche
    _notif_lancer_cycle() quand l'heure configurée arrive.
    Totalement indépendant du contexte Flask.
    """
    def _loop():
        last_run_date = None
        while True:
            try:
                _time_module.sleep(60)
                now   = datetime.now()
                today = now.date()

                _db2 = sqlite3.connect(DB_PATH)
                rows2 = _db2.execute(
                    "SELECT cle, valeur FROM parametres "
                    "WHERE cle IN ('notif_auto_active','notif_heure_envoi','notif_jours_semaine')"
                ).fetchall()
                _db2.close()
                p2 = {r[0]: r[1] for r in rows2}

                if p2.get('notif_auto_active', 'non') != 'oui':
                    continue

                heure_cfg = (p2.get('notif_heure_envoi') or '08:00').strip()
                try:
                    _h, _m = [int(x) for x in heure_cfg.split(':')]
                except Exception:
                    _h, _m = 8, 0

                jours_cfg = p2.get('notif_jours_semaine', '0123456') or '0123456'
                if str(now.weekday()) not in jours_cfg:
                    continue

                if now.hour == _h and now.minute == _m and today != last_run_date:
                    last_run_date = today
                    logging.info("[NOTIF] Déclenchement automatique %s", now.strftime('%H:%M'))
                    _notif_lancer_cycle()

            except Exception as _e2:
                logging.error("[NOTIF SCHEDULER] %s", _e2)

    import threading as _thr
    t = _thr.Thread(target=_loop, daemon=True, name='notif-scheduler')
    t.start()
    logging.info("[NOTIF] Scheduler de notifications démarré")


# Lancer le scheduler dès le chargement du module (Waitress + flask run)
_notif_demarrer_scheduler()


# ── Route : sauvegarder les paramètres notifications ─────────────────
@app.route('/parametres/notifications/save', methods=['POST'])
@login_required
def notif_save():
    if session.get('user_role') != 'admin':
        flash("Accès réservé à l'administrateur.", "danger")
        return redirect(url_for('parametres'))
    champs_txt = [
        'notif_heure_envoi', 'notif_jours_semaine',
        'notif_seuil_min', 'notif_delai_jours',
        'notif_gmail_user', 'notif_gmail_pwd',
        'notif_sujet_gmail', 'notif_corps_gmail',
        'notif_wa_phone_id', 'notif_wa_token',
        'notif_wa_template', 'notif_wa_lang',
        'notif_sms_api_url', 'notif_sms_api_key', 'notif_sms_sender',
        'notif_corps_whatsapp', 'notif_corps_sms',
    ]
    for cle in champs_txt:
        val = request.form.get(cle, '')
        execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)", (cle, val))
    for cb in ['notif_auto_active', 'notif_canal_gmail',
               'notif_canal_whatsapp', 'notif_canal_sms']:
        val = 'oui' if request.form.get(cb) == 'oui' else 'non'
        execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)", (cb, val))
    # Reconstuire jours_semaine depuis les cases cochées
    jours = ''.join(
        str(i) for i in range(7)
        if request.form.get(f'notif_jour_{i}')
    )
    if jours:
        execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)",
                ('notif_jours_semaine', jours))
    flash("✅ Paramètres de notification enregistrés.", "success")
    return redirect(url_for('parametres', tab='notifications'))


# ── Route : envoi immédiat manuel ────────────────────────────────────
@app.route('/parametres/notifications/envoyer', methods=['POST'])
@login_required
def notif_envoyer_maintenant():
    if session.get('user_role') != 'admin':
        flash("Accès réservé.", "danger")
        return redirect(url_for('parametres'))
    import threading as _thr2
    _thr2.Thread(target=_notif_lancer_cycle, daemon=True).start()
    flash("🚀 Envoi des notifications lancé en arrière-plan.", "success")
    return redirect(url_for('parametres', tab='notifications'))


# ── Route : test de connexion AJAX ───────────────────────────────────
@app.route('/parametres/notifications/test', methods=['POST'])
@login_required
def notif_test():
    if session.get('user_role') != 'admin':
        return jsonify(ok=False, msg="Accès refusé")
    canal = request.form.get('canal', 'gmail')
    rows  = query("SELECT cle, valeur FROM parametres WHERE cle LIKE 'notif_%' OR cle='nom_depot'")
    cfg_n = {r['cle']: r['valeur'] for r in rows}
    nom_depot = cfg_n.get('nom_depot', 'DISTRIGEST')

    if canal == 'gmail':
        # Toujours envoyer à l'adresse Gmail configurée (envoi à soi-même)
        gmail_user = (cfg_n.get('notif_gmail_user') or '').strip()
        u_row = query("SELECT email FROM utilisateurs WHERE id=?",
                      (session.get('user_id'),), one=True)
        u_email = ((u_row['email'] or '').strip()) if u_row else ''
        # Première adresse valide (doit contenir @)
        dest = next((e for e in [gmail_user, u_email] if e and '@' in e), '')
        if not dest:
            return jsonify(ok=False,
                           msg="Configurez d'abord l'adresse Gmail expéditrice et sauvegardez")
        ok, err = _notif_send_gmail(
            cfg_n, nom_depot, dest, 'Client Test', 75000,
            'Test DISTRIGEST — {societe}',
            'Ceci est un email de test depuis DISTRIGEST.\nSi vous recevez ce message, Gmail est correctement configuré \u2705')
    elif canal == 'whatsapp':
        tel = request.form.get('tel_test', '').strip()
        if not tel:
            return jsonify(ok=False, msg="Numéro de test requis")
        ok, err = _notif_send_whatsapp(
            cfg_n, tel, 'Test', 50000,
            '🔔 *{societe}* — Test WhatsApp DISTRIGEST ✅', nom_depot)
    elif canal == 'sms':
        tel = request.form.get('tel_test', '').strip()
        if not tel:
            return jsonify(ok=False, msg="Numéro de test requis")
        ok, err = _notif_send_sms(
            cfg_n, tel, 'Test', 50000,
            '{societe}: Test SMS DISTRIGEST envoyé avec succes.', nom_depot)
    else:
        return jsonify(ok=False, msg="Canal inconnu")

    return jsonify(ok=ok, msg=err or "Message envoyé avec succès ✅")


# ── Route : historique JSON ──────────────────────────────────────────
@app.route('/parametres/notifications/historique')
@login_required
def notif_historique():
    rows = query("""
        SELECT h.id, h.canal, h.statut, h.montant_du, h.erreur, h.date_envoi,
               c.nom AS client_nom
        FROM historique_notifications h
        LEFT JOIN clients c ON c.id = h.client_id
        ORDER BY h.date_envoi DESC
        LIMIT 200
    """)
    return jsonify([dict(r) for r in rows])


# ── Route : vider historique ─────────────────────────────────────────
@app.route('/parametres/notifications/historique/vider', methods=['POST'])
@login_required
def notif_historique_vider():
    if session.get('user_role') != 'admin':
        return jsonify(ok=False)
    execute("DELETE FROM historique_notifications")
    return jsonify(ok=True)


@app.route('/parametres/test-reseau', methods=['POST'])
@login_required
def parametres_test_reseau():
    """AJAX — Vérifie que l'hôte et le port saisis sont réels et joignables."""
    import socket, re
    if session.get('user_role') != 'admin':
        return jsonify({'ok': False, 'message': 'Accès refusé.'}), 403

    data    = request.get_json(silent=True) or {}
    host    = (data.get('host') or '').strip()
    port_s  = str(data.get('port') or '').strip()
    details = []

    # ── 1. Validation format hôte ──────────────────────────────────
    if not host:
        return jsonify({'ok': False, 'message': '❌ Hôte non renseigné.'})

    ipv4_re = re.compile(
        r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$'
    )
    hostname_re = re.compile(
        r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?'
        r'(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$'
    )

    is_wildcard = host in ('0.0.0.0', '::')
    is_loopback = host in ('127.0.0.1', 'localhost', '::1')
    is_ipv4     = bool(ipv4_re.match(host))
    is_hostname = bool(hostname_re.match(host))

    if is_ipv4:
        parts = [int(x) for x in host.split('.')]
        if not all(0 <= p <= 255 for p in parts):
            return jsonify({'ok': False, 'message': f'❌ Adresse IP invalide : {host}'})
        details.append(f'✅ Format IP valide : {host}')
    elif is_loopback:
        details.append(f'✅ Adresse locale (loopback) : {host}')
    elif is_wildcard:
        details.append(f'ℹ️  Adresse générique {host} — écoute sur toutes les interfaces')
    elif is_hostname:
        details.append(f'✅ Nom d\'hôte valide : {host}')
    else:
        return jsonify({'ok': False, 'message': f'❌ Adresse invalide : « {host} »'})

    # ── 2. Validation port ─────────────────────────────────────────
    if not port_s.isdigit():
        return jsonify({'ok': False, 'message': '❌ Port invalide (doit être un nombre).'})
    port = int(port_s)
    if not (1024 <= port <= 65535):
        return jsonify({'ok': False,
                        'message': f'❌ Port {port} hors plage autorisée (1024–65535).'})
    details.append(f'✅ Port valide : {port}')

    # ── 3. Cas adresse générique / loopback ────────────────────────
    #    0.0.0.0 et :: ne sont pas joignables depuis un autre hôte ;
    #    on teste localhost à la place pour vérifier que le serveur tourne.
    test_host = 'localhost' if is_wildcard else host

    # ── 4. Résolution DNS (si nom d'hôte) ─────────────────────────
    if not is_ipv4 and not is_wildcard and not is_loopback:
        try:
            resolved = socket.gethostbyname(test_host)
            details.append(f'✅ Résolution DNS : {test_host} → {resolved}')
        except socket.gaierror as e:
            details.append(f'❌ Résolution DNS échouée : {e}')
            return jsonify({'ok': False, 'message': '\n'.join(details)})

    # ── 5. Test TCP (connexion sur le port) ────────────────────────
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(4)
    try:
        result = sock.connect_ex((test_host, port))
        sock.close()
        if result == 0:
            details.append(f'✅ Port TCP {port} ouvert sur {test_host}')
        else:
            details.append(f'❌ Port TCP {port} fermé ou inaccessible sur {test_host} (code {result})')
            return jsonify({'ok': False, 'message': '\n'.join(details)})
    except (socket.timeout, OSError) as e:
        sock.close()
        details.append(f'❌ Connexion TCP impossible : {e}')
        return jsonify({'ok': False, 'message': '\n'.join(details)})

    # ── 6. Test HTTP — vérifie que DISTRIGEST répond ───────────────
    import urllib.request, urllib.error
    url = f'http://{test_host}:{port}/'
    try:
        req = urllib.request.Request(url, method='GET')
        req.add_header('User-Agent', 'DISTRIGEST-TestReseau/1.0')
        with urllib.request.urlopen(req, timeout=4) as resp:
            code = resp.getcode()
            details.append(f'✅ Serveur HTTP répond : HTTP {code} sur {url}')
    except urllib.error.HTTPError as e:
        # 3xx/4xx = le serveur répond quand même (login, redirect…)
        details.append(f'✅ Serveur HTTP répond : HTTP {e.code} sur {url}')
    except Exception as e:
        details.append(f'⚠️  Port ouvert mais pas de réponse HTTP : {e}')

    return jsonify({'ok': True, 'message': '\n'.join(details)})


@app.route('/parametres/module/toggle', methods=['POST'])
@login_required
def parametres_module_toggle():
    """AJAX — active/désactive un module optionnel instantanément."""
    if session.get('user_role') != 'admin':
        return ('Interdit', 403)
    data = request.get_json(silent=True) or {}
    cle = data.get('cle', '').strip()
    val = data.get('valeur', 'non').strip()
    # Whitelist des modules autorisés
    modules_autorises = {'module_caisse', 'module_emballages', 'module_atelier'}
    if cle not in modules_autorises or val not in ('oui', 'non'):
        return ('Paramètre invalide', 400)
    execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)", (cle, val))
    return ('ok', 200)

@app.route('/parametres/imprimantes')
@login_required
def parametres_imprimantes():
    """AJAX — liste les imprimantes disponibles sur le poste serveur."""
    import subprocess, platform
    imprimantes = []
    try:
        sys = platform.system()
        if sys == 'Windows':
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r'SYSTEM\CurrentControlSet\Control\Print\Printers')
            i = 0
            while True:
                try:
                    nom = winreg.EnumKey(key, i)
                    imprimantes.append({'nom': nom, 'defaut': False, 'etat': ''})
                    i += 1
                except OSError:
                    break
            # Marquer l'imprimante par défaut
            try:
                hk = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r'Software\Microsoft\Windows NT\CurrentVersion\Windows')
                defaut, _ = winreg.QueryValueEx(hk, 'Device')
                defaut_nom = defaut.split(',')[0].strip()
                for imp in imprimantes:
                    if imp['nom'] == defaut_nom:
                        imp['defaut'] = True
            except Exception:
                pass
        else:
            # Linux / macOS — lpstat
            try:
                out = subprocess.check_output(['lpstat', '-a'], timeout=4,
                                              stderr=subprocess.DEVNULL).decode('utf-8', errors='ignore')
                for line in out.strip().splitlines():
                    nom = line.split()[0] if line.strip() else None
                    if nom:
                        imprimantes.append({'nom': nom, 'defaut': False, 'etat': 'prêt'})
                # Imprimante par défaut
                try:
                    defaut = subprocess.check_output(['lpstat', '-d'], timeout=4,
                                                     stderr=subprocess.DEVNULL).decode('utf-8', errors='ignore')
                    if ':' in defaut:
                        defaut_nom = defaut.split(':', 1)[1].strip()
                        for imp in imprimantes:
                            if imp['nom'] == defaut_nom:
                                imp['defaut'] = True
                except Exception:
                    pass
            except Exception:
                pass
    except Exception as e:
        return jsonify({'imprimantes': [], 'erreur': str(e)})
    return jsonify({'imprimantes': imprimantes})


@app.route('/parametres/tester-imprimante', methods=['POST'])
@login_required
def parametres_tester_imprimante():
    """AJAX — envoie une page de test à l'imprimante configurée (temps réel)."""
    import subprocess, platform, tempfile, os as _os

    nom      = (request.json or {}).get('imprimante_nom', '').strip()
    fmt      = (request.json or {}).get('format_doc', 'a4').strip() or 'a4'

    # Contenu HTML minimal de la page de test
    test_html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  body {{ font-family: Arial, sans-serif; padding: 20px; }}
  h2   {{ color: #1e3a8a; }}
  .info {{ background: #f0f4ff; border-left: 4px solid #1e3a8a;
           padding: 12px 16px; margin-top: 16px; border-radius: 4px; }}
</style></head><body>
<h2>🖨️ Page de test — DistriGest</h2>
<p>Si vous lisez ce document, l'imprimante <strong>{nom or '(par défaut)'}</strong>
fonctionne correctement.</p>
<div class="info">
  <strong>Format :</strong> {fmt.upper()}<br>
  <strong>Date :</strong> {__import__('datetime').datetime.now().strftime('%d/%m/%Y %H:%M')}<br>
  <strong>Logiciel :</strong> DistriGest — STiNAUG TECHNOLOGIE
</div>
</body></html>"""

    sys = platform.system()

    # ── Chemin SumatraPDF (Windows) ───────────────────────────────────
    def _sumatra():
        for p in [
            r'C:\Program Files\SumatraPDF\SumatraPDF.exe',
            r'C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe',
            _os.path.join(_os.environ.get('LOCALAPPDATA',''), 'SumatraPDF', 'SumatraPDF.exe'),
            _os.path.join(_BASE_DIR, 'SumatraPDF.exe'),
            _os.path.join(_BUNDLE,   'SumatraPDF.exe'),
        ]:
            if p and _os.path.isfile(p):
                return p
        import shutil as _sh
        return _sh.which('SumatraPDF') or _sh.which('SumatraPDF.exe')

    try:
        if sys == 'Windows':
            # Écrire un PDF temporaire via weasyprint ou pdfkit, puis SumatraPDF
            with tempfile.NamedTemporaryFile(suffix='.html', delete=False, mode='w',
                                             encoding='utf-8') as fh:
                fh.write(test_html)
                html_path = fh.name

            pdf_path = html_path.replace('.html', '.pdf')
            generated = False

            # Chaîne complète : weasyprint → Edge/Chrome headless → pdfkit
            try:
                pdf_bytes, _err_pdf = _html_to_pdf_bytes(test_html, 'a4')
                if pdf_bytes:
                    with open(pdf_path, 'wb') as _fpdf:
                        _fpdf.write(pdf_bytes)
                    generated = True
            except Exception:
                pass

            if generated:
                printer = nom
                if not printer:
                    try:
                        import win32print
                        printer = win32print.GetDefaultPrinter()
                    except Exception:
                        printer = ''
                ok_snd, err_snd = _send_pdf_to_printer_windows(
                    pdf_bytes, printer, sumatra_path=_sumatra())
                try:
                    _os.unlink(html_path); _os.unlink(pdf_path)
                except Exception:
                    pass
                if ok_snd:
                    return jsonify({'ok': True, 'message': 'Page de test envoyée à l\'imprimante.'})
                return jsonify({'ok': False, 'message': err_snd})
            else:
                _os.unlink(html_path)
                return jsonify({'ok': False,
                                'message': 'Impossible de générer le PDF — aucun moteur disponible '
                                           '(Microsoft Edge introuvable, weasyprint/pdfkit absents).'})

        else:
            # Linux / macOS — lp ou lpr
            with tempfile.NamedTemporaryFile(suffix='.html', delete=False,
                                             mode='w', encoding='utf-8') as fh:
                fh.write(test_html)
                html_path = fh.name

            cmd = ['lp', '-d', nom, html_path] if nom else ['lp', html_path]
            try:
                subprocess.run(cmd, timeout=10, check=True)
            except FileNotFoundError:
                cmd2 = ['lpr', '-P', nom, html_path] if nom else ['lpr', html_path]
                subprocess.run(cmd2, timeout=10, check=True)
            _os.unlink(html_path)
            return jsonify({'ok': True, 'message': 'Page de test envoyée à l\'imprimante.'})

    except subprocess.CalledProcessError as e:
        return jsonify({'ok': False, 'message': f'Erreur impression : {e}'})
    except Exception as e:
        return jsonify({'ok': False, 'message': f'Erreur : {str(e)}'})



#  MOTEUR D'IMPRESSION DIRECTE WINDOWS
#  ─────────────────────────────────────────────────────────────────────
#  Stratégie (Windows) :
#    1. Documents PDF (A4/A5) → génération PDF via weasyprint ou pdfkit,
#       envoi à SumatraPDF -print-to <imprimante> (silencieux, zéro dialog).
#       Fallback : win32print + win32api si SumatraPDF absent.
#    2. Tickets thermiques 80mm → ESC/POS via python-escpos (USB/réseau).
#       Fallback : SumatraPDF en mode 80mm si escpos absent.
#    3. Si aucune lib n'est disponible → window.print() classique (dialog).
#  La config (imprimante_nom, imprimante_thermique_nom, sumatra_path…)
#  est stockée dans la table parametres.
# ══════════════════════════════════════════════════════════════════════

# ── Import silencieux de WeasyPrint ─────────────────────────────────
def _import_weasyprint_quiet():
    """Importe weasyprint en supprimant son bandeau console
    (« WeasyPrint could not import some external libraries… ») affiché
    sur Windows quand les bibliothèques natives GTK/Pango sont absentes.
    Retourne le module si utilisable, sinon None (sans rien afficher)."""
    import io as _io, contextlib as _ctx
    _buf = _io.StringIO()
    try:
        with _ctx.redirect_stderr(_buf), _ctx.redirect_stdout(_buf):
            import weasyprint as _wp  # noqa
        return _wp
    except Exception:
        # ImportError (absent) ou OSError (libs natives GTK manquantes)
        return None

# ── Détection des capacités disponibles sur le poste ────────────────
def _detect_print_capabilities():
    """Retourne un dict décrivant ce qui est installé sur le poste."""
    caps = {
        'weasyprint': False, 'pdfkit': False,
        'win32print': False, 'escpos': False,
        'sumatra': None,     # chemin SumatraPDF si trouvé
        'browser': None,     # chemin Edge/Chrome (rendu PDF headless)
        'platform': 'other',
    }
    import platform as _plt
    caps['platform'] = _plt.system()
    try:
        caps['browser'] = _find_browser_exe()
    except Exception:
        caps['browser'] = None
    if _import_weasyprint_quiet() is not None:
        caps['weasyprint'] = True
    try:
        import pdfkit  # noqa
        caps['pdfkit'] = True
    except ImportError:
        pass
    try:
        import win32print  # noqa
        caps['win32print'] = True
    except ImportError:
        pass
    try:
        import escpos  # noqa
        caps['escpos'] = True
    except ImportError:
        pass
    # Chercher SumatraPDF
    import os as _os
    sumatra_candidates = [
        r'C:\Program Files\SumatraPDF\SumatraPDF.exe',
        r'C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe',
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'SumatraPDF', 'SumatraPDF.exe'),
        os.path.join(os.environ.get('APPDATA', ''),      'SumatraPDF', 'SumatraPDF.exe'),
        os.path.join(_BASE_DIR, 'SumatraPDF.exe'),   # à côté de distrigest.exe
        os.path.join(_BUNDLE,   'SumatraPDF.exe'),   # embarqué PyInstaller (_internal\)
        'SumatraPDF.exe',   # dans le PATH
    ]
    for _s in sumatra_candidates:
        if _s and _os.path.isfile(_s):
            caps['sumatra'] = _s
            break
    if not caps['sumatra']:
        try:
            import shutil as _sh
            found = _sh.which('SumatraPDF') or _sh.which('SumatraPDF.exe')
            if found:
                caps['sumatra'] = found
        except Exception:
            pass
    return caps


# ── Recherche d'un navigateur Chromium (Edge / Chrome) ──────────────
_BROWSER_EXE_CACHE = '__unset__'

def _find_browser_exe():
    """Localise Microsoft Edge ou Google Chrome pour le rendu PDF headless.
    Edge est préinstallé sur Windows 10/11 → disponible sans rien installer.
    Résultat mis en cache pour éviter de rescanner le disque à chaque appel."""
    global _BROWSER_EXE_CACHE
    if _BROWSER_EXE_CACHE != '__unset__':
        return _BROWSER_EXE_CACHE
    import shutil as _sh
    _pf    = os.environ.get('ProgramFiles', r'C:\Program Files')
    _pf86  = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
    _local = os.environ.get('LOCALAPPDATA', '')
    candidates = [
        # Microsoft Edge
        os.path.join(_pf86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        os.path.join(_pf,   'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        # Google Chrome
        os.path.join(_pf,   'Google', 'Chrome', 'Application', 'chrome.exe'),
        os.path.join(_pf86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
        os.path.join(_local, 'Google', 'Chrome', 'Application', 'chrome.exe') if _local else '',
        # Linux / macOS (dev)
        '/usr/bin/microsoft-edge', '/usr/bin/google-chrome',
        '/usr/bin/chromium', '/usr/bin/chromium-browser',
        '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            _BROWSER_EXE_CACHE = c
            return c
    for name in ('msedge', 'chrome', 'chromium', 'chromium-browser',
                 'google-chrome', 'google-chrome-stable'):
        p = _sh.which(name)
        if p:
            _BROWSER_EXE_CACHE = p
            return p
    _BROWSER_EXE_CACHE = None
    return None


# Profil Edge réutilisé d'une impression à l'autre (évite l'init « 1er lancement »)
_PDF_PROFILE_DIR = None

def _get_pdf_profile_dir():
    """Profil Edge/Chrome persistant et réutilisable pour le rendu PDF.
    Le recréer à chaque appel forçait Edge à refaire son initialisation
    de premier lancement (lent). On le garde donc d'une fois sur l'autre."""
    global _PDF_PROFILE_DIR
    if _PDF_PROFILE_DIR and os.path.isdir(_PDF_PROFILE_DIR):
        return _PDF_PROFILE_DIR
    import tempfile
    base = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
    candidate = os.path.join(base, 'distrigest_pdf_profile')
    try:
        os.makedirs(candidate, exist_ok=True)
    except Exception:
        candidate = os.path.join(tempfile.gettempdir(), 'distrigest_pdf_profile')
        os.makedirs(candidate, exist_ok=True)
    _PDF_PROFILE_DIR = candidate
    return _PDF_PROFILE_DIR


def _strip_remote_fonts(html_content):
    """Retire les polices chargées depuis Internet (Google Fonts) AVANT le
    rendu PDF. Sinon Edge attend le téléchargement réseau (lent, voire bloquant
    hors-ligne en caisse). La police de repli locale (Segoe UI / Arial) suffit
    largement pour un document imprimé."""
    if not html_content:
        return html_content
    # @import url('https://fonts.googleapis.com/...');
    html_content = re.sub(
        r"@import\s+url\(\s*['\"]?https?://fonts\.(?:googleapis|gstatic)\.com[^)]*\)\s*;?",
        '', html_content, flags=re.IGNORECASE)
    # <link ... fonts.googleapis.com ...>
    html_content = re.sub(
        r"<link[^>]+fonts\.(?:googleapis|gstatic)\.com[^>]*>",
        '', html_content, flags=re.IGNORECASE)
    return html_content


def _html_to_pdf_via_browser(html_content, fmt='a4'):
    """Convertit le HTML en PDF via Edge/Chrome en mode headless (--print-to-pdf).
    Rendu fidèle au navigateur (grid, flexbox, dégradés) — aucune bibliothèque
    native requise. Sur Windows, Edge est préinstallé.

    Optimisé pour l'impression directe :
      • polices distantes retirées  -> plus d'attente réseau ;
      • budget de rendu court (1,2 s) ;
      • profil Edge persistant       -> plus d'init « 1er lancement » à chaque fois."""
    browser = _find_browser_exe()
    if not browser:
        return None, "Aucun navigateur Edge/Chrome trouvé pour le rendu PDF"

    import subprocess, tempfile, shutil

    # ① Couper les polices distantes => plus d'attente réseau
    html_content = _strip_remote_fonts(html_content)

    tmpdir    = tempfile.mkdtemp(prefix='dg_pdf_')
    html_path = os.path.join(tmpdir, 'doc.html')
    pdf_path  = os.path.join(tmpdir, 'doc.pdf')

    # ② Profil persistant (init « 1er lancement » payée une seule fois)
    profile = _get_pdf_profile_dir()

    # Évite qu'une fenêtre console clignote sous Windows
    _flags = 0
    if os.name == 'nt':
        _flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

    try:
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        url = 'file:///' + html_path.replace('\\', '/')

        common = [
            '--disable-gpu', '--no-sandbox', '--disable-extensions',
            '--disable-background-networking', '--disable-sync',
            '--disable-component-update', '--disable-default-apps',
            '--no-first-run', '--no-default-browser-check', '--no-pings',
            f'--user-data-dir={profile}',            # profil réutilisé d'un appel à l'autre
            '--run-all-compositor-stages-before-draw',
            '--virtual-time-budget=1200',            # ③ 5000 -> 1200 ms (sans police réseau)
            '--no-pdf-header-footer',                 # pas d'en-tête/pied de page navigateur
            f'--print-to-pdf={pdf_path}',
            url,
        ]
        # Edge/Chrome récents : --headless=new ; anciens : --headless
        for headless_flag in ('--headless=new', '--headless'):
            try:
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)
            except Exception:
                pass
            args = [browser, headless_flag] + common
            try:
                subprocess.run(args, capture_output=True, timeout=30,
                               creationflags=_flags)
            except Exception:
                # Profil persistant verrouillé (impression simultanée) :
                # on retente avec un profil jetable propre à cet appel.
                fallback_profile = os.path.join(tmpdir, 'profile')
                args = [browser, headless_flag] + [
                    a if not a.startswith('--user-data-dir=')
                    else f'--user-data-dir={fallback_profile}'
                    for a in common
                ]
                try:
                    subprocess.run(args, capture_output=True, timeout=30,
                                   creationflags=_flags)
                except Exception:
                    continue
            if os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 100:
                with open(pdf_path, 'rb') as f:
                    return f.read(), None
        return None, "Le navigateur n'a pas pu générer le PDF (mode headless)"
    except Exception as exc:
        return None, f"Rendu navigateur : {exc}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _html_to_pdf_bytes(html_content, fmt='a4'):
    """Convertit du HTML en PDF (bytes).

    Ordre des moteurs (du plus fidèle/léger au moins) :
      1. weasyprint            — si les libs natives sont présentes
      2. Edge/Chrome headless  — fidélité maximale, Edge préinstallé sur Windows
      3. pdfkit / wkhtmltopdf  — dernier recours (rendu CSS limité)
    """
    # ── 1) weasyprint ────────────────────────────────────────────────
    _wp = _import_weasyprint_quiet()
    if _wp is not None:
        try:
            css_page = f'@page {{ size: {fmt.upper() if fmt != "80mm" else "80mm auto"}; margin: 10mm; }}'
            pdf = _wp.HTML(string=html_content, base_url=None).write_pdf(
                stylesheets=[_wp.CSS(string=css_page)])
            if pdf:
                return pdf, None
        except Exception:
            pass

    # ── 2) Edge / Chrome headless (rendu identique au navigateur) ─────
    pdf, err_browser = _html_to_pdf_via_browser(html_content, fmt)
    if pdf:
        return pdf, None

    # ── 3) pdfkit / wkhtmltopdf ──────────────────────────────────────
    try:
        import pdfkit as _pk
        opts = {'page-size': fmt.upper() if fmt not in ('80mm',) else 'A4',
                'encoding': 'UTF-8', 'quiet': '',
                'print-media-type': '',
                'enable-local-file-access': ''}
        if fmt == '80mm':
            opts.update({'page-width': '80mm', 'page-height': '297mm',
                         'margin-top': '3mm', 'margin-bottom': '3mm',
                         'margin-left': '3mm', 'margin-right': '3mm'})
        pdf = _pk.from_string(html_content, False, options=opts)
        if pdf:
            return pdf, None
    except Exception:
        pass

    # ── Aucun moteur n'a abouti ──────────────────────────────────────
    return None, (err_browser or
                  "Aucun moteur PDF disponible. Installez Microsoft Edge "
                  "(préinstallé sur Windows 10/11), ou weasyprint, ou wkhtmltopdf.")


def _send_pdf_to_printer_windows(pdf_bytes, printer_name, sumatra_path=None, copies=1):
    """
    Envoie un PDF à une imprimante Windows sans dialog.
    Stratégie 1 : SumatraPDF -print-to <nom> (silencieux, gère les copies).
    Stratégie 2 : win32print (RAW — imprimante doit accepter PDF natif).
    """
    import tempfile, os as _os, subprocess as _sp
    # Écrire le PDF dans un fichier temporaire
    with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf',
                                     dir=_os.environ.get('TEMP', _BASE_DIR)) as tf:
        tf.write(pdf_bytes)
        tmp_path = tf.name

    try:
        # ── Stratégie 1 : SumatraPDF ────────────────────────────────
        if sumatra_path and _os.path.isfile(sumatra_path):
            cmd = [
                sumatra_path,
                '-print-to', printer_name,
                '-print-settings', f'{copies}x',  # nombre de copies
                '-silent',    # pas de GUI
                tmp_path,
            ]
            proc = _sp.run(cmd, timeout=30,
                           stdout=_sp.DEVNULL, stderr=_sp.PIPE,
                           creationflags=getattr(_sp, 'CREATE_NO_WINDOW', 0))
            if proc.returncode == 0:
                return True, None
            err_txt = (proc.stderr or b'').decode('utf-8', 'replace').strip()
            # SumatraPDF retourne parfois 1 même si ça a marché
            if not err_txt:
                return True, None
            # Continuer vers stratégie 2 si erreur réelle
            logging.warning("[PRINT] SumatraPDF exit %d : %s", proc.returncode, err_txt)

        # ── Stratégie 2 : win32print (RAW) ──────────────────────────
        err_raw = None
        try:
            import win32print
            hPrinter = win32print.OpenPrinter(printer_name)
            try:
                win32print.StartDocPrinter(hPrinter, 1, ('DISTRIGEST', None, 'RAW'))
                win32print.StartPagePrinter(hPrinter)
                win32print.WritePrinter(hPrinter, pdf_bytes)
                win32print.EndPagePrinter(hPrinter)
                win32print.EndDocPrinter(hPrinter)
            finally:
                win32print.ClosePrinter(hPrinter)
            return True, None
        except ImportError:
            pass
        except Exception as exc_w:
            # Ne pas abandonner : tenter encore la stratégie 3
            err_raw = f"win32print : {exc_w}"
            logging.warning("[PRINT] %s", err_raw)

        # ── Stratégie 3 : ShellExecute print (dernier recours) ──────
        try:
            import win32api
            win32api.ShellExecute(0, 'print', tmp_path, f'/d:"{printer_name}"', '.', 0)
            return True, None
        except ImportError:
            pass
        except Exception as exc_s:
            # Erreur 31 = aucune application associée au verbe « print »
            # pour les PDF (pas d'Adobe Reader ni SumatraPDF sur le poste).
            code = getattr(exc_s, 'winerror', None) or \
                   (exc_s.args[0] if exc_s.args and isinstance(exc_s.args[0], int) else None)
            if code == 31:
                return False, ("Aucune application d'impression PDF n'est installée sur ce poste. "
                               "Installez SumatraPDF (gratuit, portable — sumatrapdfreader.org) "
                               "puis renseignez son chemin dans Paramètres → Impression, "
                               "ou utilisez l'impression navigateur (Ctrl+P).")
            return False, (err_raw or f"ShellExecute : {exc_s}")

        return False, (err_raw or
                       "Aucune méthode d'impression disponible (SumatraPDF, win32print ou pywin32 requis)")
    finally:
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass


def _build_escpos_ticket(doc_id, doc_type, cfg_print, cfg_app):
    """
    Génère et envoie un ticket ESC/POS à l'imprimante thermique.
    Connexion : USB (vendor_id/product_id) ou réseau (IP:port).
    doc_type : 'facture' | 'bl' | 'devis' | 'commande' | 'recu'
    """
    try:
        from escpos import printer as _epr
    except ImportError:
        return False, "python-escpos non installé (pip install python-escpos)"

    # ── Connexion à l'imprimante thermique ──────────────────────────
    therm_mode   = (cfg_print.get('thermique_mode') or 'usb').strip().lower()
    therm_ip     = (cfg_print.get('thermique_ip') or '').strip()
    therm_port   = int(cfg_print.get('thermique_port') or 9100)
    therm_vendor = cfg_print.get('thermique_usb_vendor') or ''
    therm_prod   = cfg_print.get('thermique_usb_product') or ''

    try:
        if therm_mode == 'reseau' and therm_ip:
            p = _epr.Network(therm_ip, therm_port, timeout=5)
        elif therm_vendor and therm_prod:
            p = _epr.Usb(int(therm_vendor, 16), int(therm_prod, 16), timeout=5)
        else:
            p = _epr.Usb(0x0416, 0x5011, timeout=5)   # POS-80 générique
    except Exception as exc_conn:
        return False, f"Connexion imprimante thermique : {exc_conn}"

    # ── Récupérer les données du document ───────────────────────────
    def _fcfa(v):
        try: return f"{int(float(v or 0)):,}".replace(',', ' ')
        except: return '0'

    nom_soc = cfg_app.get('nom_depot', 'MON COMMERCE')
    tel_soc = cfg_app.get('telephone', '')
    adr_soc = cfg_app.get('adresse', '')
    devise  = cfg_app.get('devise', 'FCFA')
    now_str = datetime.now().strftime('%d/%m/%Y %H:%M')

    if doc_type in ('facture', 'devis', 'commande', 'recu'):
        doc = query("""SELECT dv.*, c.nom as client_nom
                       FROM documents_vente dv
                       LEFT JOIN clients c ON c.id=dv.client_id
                       WHERE dv.id=?""", (doc_id,), one=True)
        if not doc:
            return False, "Document introuvable"
        doc = dict(doc)
        lignes = [dict(r) for r in query("""
            SELECT COALESCE(lv.designation, a.designation) as designation,
                   lv.quantite_unite, lv.total_ttc
            FROM lignes_vente lv
            LEFT JOIN articles a ON a.id=lv.article_id
            WHERE lv.document_id=? ORDER BY lv.num_ligne""", (doc_id,))]
        client_nom = doc.get('client_nom') or 'Client passager'
        ref        = doc.get('reference', '')
        total_ttc  = doc.get('total_ttc', 0)
        reste      = doc.get('reste', 0)
        titre_doc  = {'facture':'FACTURE','devis':'DEVIS',
                      'commande':'COMMANDE','recu':'REÇU'}.get(doc_type, 'DOCUMENT')

    elif doc_type == 'bl':
        doc = query("""SELECT bl.*, c.nom as client_nom
                       FROM bons_livraison bl
                       LEFT JOIN clients c ON c.id=bl.client_id
                       WHERE bl.id=?""", (doc_id,), one=True)
        if not doc:
            return False, "BL introuvable"
        doc = dict(doc)
        lignes = [dict(r) for r in query("""
            SELECT COALESCE(lb.designation, a.designation) as designation,
                   lb.quantite_livree as quantite_unite, lb.total_ttc
            FROM lignes_bl lb
            LEFT JOIN articles a ON a.id=lb.article_id
            WHERE lb.bl_id=? ORDER BY lb.num_ligne""", (doc_id,))]
        client_nom = doc.get('client_nom') or '—'
        ref        = doc.get('reference', '')
        total_ttc  = doc.get('total_ttc', 0)
        reste      = 0
        titre_doc  = 'BON DE LIVRAISON'
    else:
        return False, f"Type de document inconnu : {doc_type}"

    # ── Construction du ticket ESC/POS ──────────────────────────────
    SEP = '-' * 32

    try:
        p.set(align='center', bold=True, height=2, width=2)
        p.text(nom_soc[:20] + '\n')
        p.set(align='center', bold=False, height=1, width=1)
        _soc_t = _infos_entreprise()
        if _soc_t['adresse']: p.text(_soc_t['adresse'][:32] + '\n')
        elif adr_soc:         p.text(adr_soc[:32] + '\n')
        if tel_soc:           p.text('Tel : ' + tel_soc[:24] + '\n')
        if _soc_t['email']:   p.text(_soc_t['email'][:32] + '\n')
        if _soc_t['ncc']:     p.text('NCC : '  + _soc_t['ncc'][:24]  + '\n')
        if _soc_t['rccm']:    p.text('RCCM : ' + _soc_t['rccm'][:24] + '\n')
        p.text(SEP + '\n')

        p.set(align='center', bold=True)
        p.text(titre_doc + '\n')
        p.set(align='center', bold=False)
        p.text(ref + '\n')
        p.text(now_str + '\n')
        p.text('Client : ' + client_nom[:22] + '\n')
        p.text(SEP + '\n')

        p.set(align='left', bold=False)
        for l in lignes:
            des = (l.get('designation') or '—')[:22]
            qte = int(float(l.get('quantite_unite') or 1))
            ttc = _fcfa(l.get('total_ttc', 0))
            line = f"{des:<22} x{qte}\n"
            p.text(line)
            p.text(f"{'':>22} {ttc:>8} {devise}\n")

        p.text(SEP + '\n')
        p.set(align='right', bold=True, height=1, width=1)
        p.text(f"TOTAL TTC : {_fcfa(total_ttc)} {devise}\n")
        if float(reste or 0) > 0:
            p.set(align='right', bold=True)
            p.text(f"RESTE     : {_fcfa(reste)} {devise}\n")
        p.text(SEP + '\n')
        p.set(align='center', bold=False, height=1, width=1)
        p.text('Merci pour votre confiance !\n')
        p.text('DISTRIGEST · STiNAUG TECHNOLOGIE\n')
        p.cut()
        return True, None
    except Exception as exc_p:
        return False, f"Impression ESC/POS : {exc_p}"


# ── Tiroir-caisse : ouverture via impulsion ESC/POS ─────────────────
def _ouvrir_tiroir_caisse(cfg_print=None):
    """Ouvre le tiroir-caisse relié à l'imprimante thermique.

    Le tiroir est branché sur le port RJ11/RJ12 de l'imprimante ticket, et
    l'imprimante est reliée au PC en USB. On lui envoie l'impulsion
    d'ouverture (« kick », commande ESC p).

    Deux stratégies, dans l'ordre :
      1. win32print RAW vers le NOM WINDOWS de l'imprimante (recommandé quand
         l'imprimante USB est installée avec son pilote Windows normal — le
         cas le plus courant ; n'exige NI libusb NI Zadig).
      2. python-escpos (USB libusb / réseau) en repli.

    Retourne (ok: bool, message_erreur: str|None).
    """
    if cfg_print is None:
        cfg_print = {r['cle']: r['valeur'] for r in
                     query("SELECT cle,valeur FROM parametres "
                           "WHERE cle LIKE 'imprimante%' OR cle LIKE 'thermique%' "
                           "OR cle LIKE 'tiroir%'")}

    # Broche d'ouverture : 2 (défaut) ou 5 selon le câblage du tiroir
    try:
        pin = int(cfg_print.get('tiroir_pin') or 2)
    except (ValueError, TypeError):
        pin = 2
    if pin not in (2, 5):
        pin = 2

    # Impulsion ESC p m t1 t2 (m=0 → broche 2 ; m=1 → broche 5)
    kick = b'\x1b\x70\x01\x19\xfa' if pin == 5 else b'\x1b\x70\x00\x19\xfa'

    errs = []

    # ── Stratégie 1 : win32print RAW vers le nom Windows (USB/Windows) ──
    try:
        import platform as _plt
        if _plt.system() == 'Windows':
            try:
                import win32print
                nom = (cfg_print.get('thermique_nom')
                       or cfg_print.get('imprimante_thermique_nom')
                       or cfg_print.get('imprimante_nom') or '').strip()
                if not nom:
                    nom = win32print.GetDefaultPrinter()
                h = win32print.OpenPrinter(nom)
                try:
                    win32print.StartDocPrinter(h, 1, ('DISTRIGEST-Tiroir', None, 'RAW'))
                    win32print.StartPagePrinter(h)
                    win32print.WritePrinter(h, kick)
                    win32print.EndPagePrinter(h)
                    win32print.EndDocPrinter(h)
                finally:
                    win32print.ClosePrinter(h)
                return True, None
            except Exception as exc_w:
                errs.append(f"win32print : {exc_w}")
    except Exception:
        pass

    # ── Stratégie 2 : python-escpos (USB libusb / réseau) ──
    try:
        from escpos import printer as _epr
    except ImportError:
        if errs:
            return False, errs[0] + " · python-escpos absent pour le repli."
        return False, "python-escpos non installé (pip install python-escpos)"

    therm_mode   = (cfg_print.get('thermique_mode') or 'usb').strip().lower()
    therm_ip     = (cfg_print.get('thermique_ip') or '').strip()
    therm_port   = int(cfg_print.get('thermique_port') or 9100)
    therm_vendor = cfg_print.get('thermique_usb_vendor') or ''
    therm_prod   = cfg_print.get('thermique_usb_product') or ''

    try:
        if therm_mode == 'reseau' and therm_ip:
            p = _epr.Network(therm_ip, therm_port, timeout=5)
        elif therm_vendor and therm_prod:
            p = _epr.Usb(int(therm_vendor, 16), int(therm_prod, 16), timeout=5)
        else:
            p = _epr.Usb(0x0416, 0x5011, timeout=5)   # POS-80 générique
    except Exception as exc_conn:
        errs.append(f"escpos connexion : {exc_conn}")
        return False, ' · '.join(errs)

    try:
        try:
            p.cashdraw(pin)                       # impulsion standard ESC p
        except Exception:
            p._raw(kick)                          # repli : octets bruts
        try:
            p.close()
        except Exception:
            pass
        return True, None
    except Exception as exc_p:
        errs.append(f"escpos : {exc_p}")
        return False, ' · '.join(errs)


# ── Route : ouverture manuelle du tiroir-caisse (bouton 🗄️ Tiroir) ──
@app.route('/caisse/tiroir/ouvrir', methods=['POST'])
@login_required
def caisse_tiroir_ouvrir():
    """Ouvre manuellement le tiroir-caisse. Réservé à l'administrateur.
    Contrat front (caisse.html) : renvoie {ok:true,msg} ou {ok:false,err}."""
    if session.get('user_role') != 'admin':
        return jsonify(ok=False, err="Action réservée à l'administrateur."), 403
    cfg = get_cfg()
    if cfg.get('tiroir_caisse') != 'oui':
        return jsonify(ok=False, err="Tiroir-caisse désactivé — activez-le dans "
                                     "Paramètres → Impression."), 400
    _caps = _detect_print_capabilities()
    if not (_caps['escpos'] or _caps['win32print']):
        return jsonify(ok=False, err="Aucun pilote d'impression disponible (pywin32 ou "
                                     "python-escpos requis)."), 400
    ok, err = _ouvrir_tiroir_caisse()
    if ok:
        logging.info("[TIROIR] Ouverture manuelle par %s", session.get('user') or '?')
        return jsonify(ok=True, msg="Tiroir ouvert")
    logging.warning("[TIROIR] Ouverture impossible : %s", err)
    return jsonify(ok=False, err=err or "Ouverture impossible")


# ── Route : test du tiroir depuis Paramètres → Impression ───────────
@app.route('/parametres/tester-tiroir', methods=['POST'])
@login_required
def parametres_tester_tiroir():
    """Envoie une impulsion d'ouverture pour vérifier le câblage du tiroir.
    Contrat front (parametres.html) : renvoie {ok, message}."""
    if session.get('user_role') != 'admin':
        return jsonify(ok=False, message="Action réservée à l'administrateur."), 403
    _caps = _detect_print_capabilities()
    if not (_caps['escpos'] or _caps['win32print']):
        return jsonify(ok=False, message="Aucun pilote d'impression disponible — installez "
                                         "pywin32 (Windows) ou python-escpos.")
    ok, err = _ouvrir_tiroir_caisse()
    if ok:
        return jsonify(ok=True, message="Impulsion envoyée — le tiroir doit s'ouvrir.")
    return jsonify(ok=False, message=err or "Ouverture impossible.")


# ── Route principale : impression directe ───────────────────────────
@app.route('/impression/directe/<doc_type>/<int:doc_id>', methods=['POST'])
@login_required
def impression_directe(doc_type, doc_id):
    """
    Génère et envoie le document directement à l'imprimante sans dialog navigateur.
    doc_type : facture | devis | commande | bl | achat | recu
    Corps JSON attendu : { "mode": "pdf" | "thermique" }
    """
    data         = request.get_json(silent=True) or {}
    mode_demande = (data.get('mode') or 'pdf').strip().lower()

    cfg_app   = get_cfg()
    cfg_print = {r['cle']: r['valeur'] for r in
                 query("SELECT cle,valeur FROM parametres WHERE cle LIKE 'imprimante%' OR cle LIKE 'thermique%'")}

    printer_nom   = (cfg_print.get('imprimante_nom') or '').strip()
    copies        = max(1, int(cfg_print.get('imprimante_copies') or 1))
    fmt           = _norm_print_format(cfg_print.get('imprimante_type') or 'a4')
    caps          = _detect_print_capabilities()
    sumatra_path  = cfg_print.get('sumatra_path') or caps['sumatra']

    # ── Mode thermique ──────────────────────────────────────────────
    if mode_demande == 'thermique' or fmt == '80mm':
        ok, err = _build_escpos_ticket(doc_id, doc_type, cfg_print, cfg_app)
        if not ok and fmt == '80mm' and caps['sumatra']:
            # Fallback : SumatraPDF en mode 80mm
            mode_demande = 'pdf'
        elif not ok:
            return jsonify(ok=False, err=err,
                           fallback='window_print',
                           msg=f"Impression thermique impossible : {err}. Utilisez le dialog navigateur.")
        else:
            return jsonify(ok=True, msg="Ticket imprimé ✅")

    # ── Mode PDF (A4/A5/A6) ─────────────────────────────────────────
    # 1. Générer le HTML du document
    _doc_route_map = {
        'facture':  lambda: _print_document_vente(doc_id, 'FACTURE'),
        'devis':    lambda: _print_document_vente(doc_id, 'DEVIS'),
        'commande': lambda: _print_document_vente(doc_id, 'COMMANDE'),
        'bl':       lambda: _print_document_bl(doc_id),
        'achat':    lambda: _print_document_achat(doc_id),
    }
    gen = _doc_route_map.get(doc_type)
    if not gen:
        return jsonify(ok=False, err=f"Type de document inconnu : {doc_type}")

    try:
        resp = gen()
        if isinstance(resp, tuple):
            return jsonify(ok=False, err=f"Document introuvable (code {resp[1]})")
        html_content = resp if isinstance(resp, str) else resp.get_data(as_text=True)
    except Exception as exc_gen:
        return jsonify(ok=False, err=f"Génération HTML : {exc_gen}",
                       fallback='window_print')

    # 2. HTML → PDF
    pdf_bytes, err_pdf = _html_to_pdf_bytes(html_content, fmt)
    if not pdf_bytes:
        return jsonify(ok=False, err=err_pdf, fallback='window_print',
                       msg=f"Génération PDF impossible : {err_pdf}. "
                           f"Installez weasyprint ou pdfkit.")

    # 3. PDF → imprimante
    if not printer_nom:
        # Récupérer l'imprimante par défaut Windows
        try:
            import win32print
            printer_nom = win32print.GetDefaultPrinter()
        except Exception:
            return jsonify(ok=False, err="Aucune imprimante configurée",
                           fallback='window_print',
                           msg="Configurez l'imprimante dans Paramètres → Imprimante.")

    if caps['platform'] != 'Windows':
        return jsonify(ok=False, err="Impression directe disponible sur Windows uniquement",
                       fallback='window_print')

    ok, err_snd = _send_pdf_to_printer_windows(pdf_bytes, printer_nom, sumatra_path, copies)
    if ok:
        logging.info("[PRINT] %s #%d → %s (%s)", doc_type, doc_id, printer_nom, fmt)
        return jsonify(ok=True, msg=f"Document envoyé à « {printer_nom} » ✅")
    else:
        return jsonify(ok=False, err=err_snd, fallback='window_print',
                       msg=f"Erreur envoi imprimante : {err_snd}")


# ── Route : statut & capacités impression ───────────────────────────
@app.route('/impression/statut')
@login_required
def impression_statut():
    """AJAX — retourne les capacités d'impression du poste + config actuelle."""
    caps = _detect_print_capabilities()
    cfg_print = {r['cle']: r['valeur'] for r in
                 query("SELECT cle,valeur FROM parametres WHERE cle LIKE 'imprimante%' OR cle LIKE 'thermique%'")}
    printer_nom = (cfg_print.get('imprimante_nom') or '').strip()
    if not printer_nom:
        try:
            import win32print
            printer_nom = win32print.GetDefaultPrinter()
        except Exception:
            printer_nom = ''
    can_direct = (caps['platform'] == 'Windows' and
                  (caps['weasyprint'] or caps['pdfkit'] or caps['browser']) and
                  (caps['sumatra'] or caps['win32print']))
    can_thermal = caps['escpos']
    return jsonify({
        'can_direct':  can_direct,
        'can_thermal': can_thermal,
        'printer':     printer_nom,
        'sumatra':     caps['sumatra'],
        'browser':     caps['browser'],
        'weasyprint':  caps['weasyprint'],
        'pdfkit':      caps['pdfkit'],
        'escpos':      caps['escpos'],
        'platform':    caps['platform'],
        'fmt':         cfg_print.get('imprimante_type', 'a4'),
        'install_guide': {
            'weasyprint': 'pip install weasyprint',
            'pdfkit':     'pip install pdfkit  +  wkhtmltopdf.org',
            'escpos':     'pip install python-escpos',
            'sumatra':    'https://www.sumatrapdfreader.org/download-free-pdf-viewer',
        }
    })


# ── Route : enregistrer le chemin SumatraPDF ─────────────────────────
@app.route('/parametres/sumatra', methods=['POST'])
@login_required
def parametres_sumatra_save():
    if session.get('user_role') != 'admin':
        return jsonify(ok=False, msg="Accès refusé")
    chemin = (request.get_json(silent=True) or {}).get('chemin', '').strip()
    execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)",
            ('sumatra_path', chemin))
    return jsonify(ok=True)


# ── Injection du bouton "Imprimer directement" dans les pages HTML ───
_DIRECT_PRINT_JS = r"""
<script id="distrigest-direct-print">
(function(){
  /* ══ Moteur d'impression directe DISTRIGEST ══
     Injecté dans toutes les pages d'impression.
     Remplace le dialog navigateur par un appel
     à l'API /impression/directe/<type>/<id>.    */

  var _docType = document.currentScript
              ? document.currentScript.getAttribute('data-doc-type') : null;
  var _docId   = document.currentScript
              ? document.currentScript.getAttribute('data-doc-id')   : null;

  /* ── Détection côté serveur au chargement ─────────────────────── */
  var _caps = null;
  async function _loadCaps(){
    try {
      var r = await fetch('/impression/statut');
      _caps = await r.json();
    } catch(e){ _caps = {can_direct:false, can_thermal:false}; }
    _injectButtons();
  }

  /* ── Injection des boutons dans .no-print ─────────────────────── */
  function _injectButtons(){
    if(!_caps) return;
    var bar = document.querySelector('.no-print');
    if(!bar) return;

    /* Supprimer l'ancien bouton Imprimer (window.print) */
    var oldBtn = bar.querySelector('.btn-print');
    if(oldBtn) oldBtn.style.display = 'none';

    var container = document.createElement('div');
    container.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;';

    /* ─── Bouton PDF direct ─────────────────────────────────────── */
    var btnPdf = document.createElement('button');
    btnPdf.className = 'btn-print';
    if(_caps.can_direct){
      btnPdf.innerHTML = '🖨️ Imprimer directement';
      btnPdf.title     = 'Imprimante : ' + (_caps.printer || 'par défaut') +
                         '\nFormat : ' + (_caps.fmt || 'A4').toUpperCase();
      btnPdf.style.background = 'linear-gradient(135deg,#166534,#16a34a)';
      btnPdf.onclick = function(){ _sendToPrinter('pdf'); };
    } else {
      btnPdf.innerHTML = '🖨️ Imprimer (dialog)';
      btnPdf.title     = 'Impression directe non disponible — dialog navigateur';
      btnPdf.onclick   = function(){ window.print(); };
    }
    container.appendChild(btnPdf);

    /* ─── Bouton ticket thermique ───────────────────────────────── */
    if(_docType && _docId && (_caps.can_thermal || _caps.can_direct)){
      var fmt = (_caps.fmt || '').toLowerCase();
      var btnTherm = document.createElement('button');
      btnTherm.className = 'btn-print';
      btnTherm.innerHTML = '🧾 Ticket thermique';
      btnTherm.style.cssText = 'background:linear-gradient(135deg,#92400e,#d97706);';
      btnTherm.title = _caps.can_thermal
        ? 'Envoi ESC/POS vers imprimante thermique'
        : 'Mode 80mm via SumatraPDF';
      btnTherm.onclick = function(){ _sendToPrinter('thermique'); };
      container.appendChild(btnTherm);
    }

    /* ─── Info imprimante ───────────────────────────────────────── */
    if(_caps.printer){
      var info = document.createElement('span');
      info.style.cssText = 'font-size:11px;color:#64748b;font-weight:500;';
      info.textContent   = '🖨 ' + _caps.printer;
      container.appendChild(info);
    } else if(!_caps.can_direct){
      var warn = document.createElement('a');
      warn.href  = '/parametres?tab=imprimante';
      warn.style.cssText = 'font-size:11px;color:#d97706;font-weight:600;';
      warn.textContent   = '⚠ Configurer imprimante';
      warn.target = '_blank';
      container.appendChild(warn);
    }

    bar.insertBefore(container, bar.firstChild);

    /* Garder le bouton Fermer */
    var closeBtn = bar.querySelector('.btn-close');
    if(closeBtn) bar.appendChild(closeBtn);
  }

  /* ── Appel API impression directe ─────────────────────────────── */
  async function _sendToPrinter(mode){
    if(!_docType || !_docId){
      window.print(); return;
    }
    var allBtns = document.querySelectorAll('.no-print button');
    allBtns.forEach(function(b){ b.disabled=true; b.style.opacity='.5'; });

    var statusEl = document.querySelector('#print-status');
    if(!statusEl){
      statusEl = document.createElement('div');
      statusEl.id = 'print-status';
      statusEl.style.cssText = 'margin-top:8px;font-size:12px;font-weight:600;';
      var bar = document.querySelector('.no-print');
      if(bar) bar.appendChild(statusEl);
    }
    statusEl.style.color = '#2563eb';
    statusEl.textContent = '⏳ Envoi en cours…';

    try {
      var r = await fetch('/impression/directe/' + _docType + '/' + _docId, {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({mode: mode})
      });
      var data = await r.json();
      if(data.ok){
        statusEl.style.color = '#166534';
        statusEl.textContent = '✅ ' + (data.msg || 'Imprimé avec succès');
      } else {
        statusEl.style.color = '#dc2626';
        statusEl.textContent = '⚠ ' + (data.msg || data.err || 'Erreur impression');
        if(data.fallback === 'window_print'){
          setTimeout(function(){
            statusEl.textContent += ' — Ouverture dialog navigateur…';
            window.print();
          }, 1200);
        }
      }
    } catch(e){
      statusEl.style.color = '#dc2626';
      statusEl.textContent = '⚠ Erreur réseau — dialog navigateur…';
      setTimeout(function(){ window.print(); }, 1200);
    } finally {
      setTimeout(function(){
        allBtns.forEach(function(b){ b.disabled=false; b.style.opacity=''; });
      }, 3000);
    }
  }

  /* Lancer au chargement de la page */
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', _loadCaps);
  } else {
    _loadCaps();
  }
})();
</script>
"""

def _inject_direct_print_script(html_content, doc_type, doc_id):
    """
    Injecte le script JS d'impression directe dans une page HTML générée,
    avec les attributs data-doc-type et data-doc-id renseignés.
    Idempotent (vérifie la présence de distrigest-direct-print).
    """
    if not html_content or 'distrigest-direct-print' in html_content:
        return html_content
    snippet = _DIRECT_PRINT_JS.replace(
        'id="distrigest-direct-print"',
        f'id="distrigest-direct-print" data-doc-type="{doc_type}" data-doc-id="{doc_id}"'
    )
    idx = html_content.lower().rfind('</body>')
    if idx != -1:
        return html_content[:idx] + snippet + html_content[idx:]
    return html_content + snippet


# ── Réponse PDF directe : ouvre le document PDF dans le navigateur ───
def _impression_mode():
    """Mode d'impression global configuré dans Paramètres → Impression.
      'apercu'  (défaut) : le document s'ouvre en PDF/HTML dans le navigateur.
      'directe'          : envoi silencieux à l'imprimante configurée.
    Échappatoires URL : ?apercu=1 force l'aperçu, ?download=1 force le
    téléchargement, ?html=1 force la page HTML — quel que soit le mode."""
    try:
        cfg = get_cfg()
        m = (cfg.get('impression_mode') or 'apercu').strip().lower()
        return m if m in ('apercu', 'directe') else 'apercu'
    except Exception:
        return 'apercu'


def _envoyer_pdf_imprimante_configuree(pdf_bytes):
    """Envoie un PDF à l'imprimante définie dans Paramètres → Impression.
    Retourne (ok, nom_imprimante, message_erreur)."""
    cfg_print = {r['cle']: r['valeur'] for r in
                 query("SELECT cle,valeur FROM parametres "
                       "WHERE cle LIKE 'imprimante%' OR cle='sumatra_path'")}
    printer_nom = (cfg_print.get('imprimante_nom') or '').strip()
    copies      = max(1, int(cfg_print.get('imprimante_copies') or 1))
    caps        = _detect_print_capabilities()
    sumatra     = cfg_print.get('sumatra_path') or caps['sumatra']

    if caps['platform'] != 'Windows':
        return False, printer_nom, "Impression directe disponible sous Windows uniquement"
    if not printer_nom:
        try:
            import win32print
            printer_nom = win32print.GetDefaultPrinter()
        except Exception:
            return False, '', ("Aucune imprimante configurée — renseignez-la dans "
                               "Paramètres → Impression.")
    ok, err = _send_pdf_to_printer_windows(pdf_bytes, printer_nom,
                                           sumatra_path=sumatra, copies=copies)
    return ok, printer_nom, err


def _page_confirmation_impression(ok, printer_nom, err=None, fallback_url=None):
    """Petite page HTML affichée après un envoi direct à l'imprimante
    (l'onglet se referme tout seul si l'envoi a réussi)."""
    if ok:
        titre, icone, couleur = "Document envoyé à l'imprimante", "🖨️", "#16a34a"
        detail = f"Imprimante : <strong>{printer_nom}</strong>"
        script = "<script>setTimeout(function(){ window.close(); }, 1800);</script>"
    else:
        titre, icone, couleur = "Impression directe impossible", "⚠️", "#dc2626"
        detail = (err or "Erreur inconnue")
        if fallback_url:
            detail += (f'<br><br><a href="{fallback_url}" '
                       f'style="color:#2563eb;font-weight:700;">→ Ouvrir l\'aperçu PDF '
                       f'pour imprimer via le navigateur (Ctrl+P)</a>')
        script = ""
    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<title>Impression — DISTRIGEST</title></head>
<body style="font-family:'Inter',-apple-system,sans-serif;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0;background:#f1f5f9;">
<div style="background:white;border-radius:14px;padding:36px 44px;text-align:center;
box-shadow:0 8px 32px rgba(0,0,0,.10);max-width:440px;">
<div style="font-size:46px;margin-bottom:14px;">{icone}</div>
<div style="font-size:17px;font-weight:800;color:{couleur};margin-bottom:8px;">{titre}</div>
<div style="font-size:13px;color:#475569;line-height:1.6;">{detail}</div>
<button onclick="window.close()" style="margin-top:22px;padding:9px 22px;border:none;
border-radius:8px;background:#1a3a6c;color:white;font-weight:700;font-size:13px;
cursor:pointer;font-family:inherit;">Fermer</button>
</div>{script}</body></html>"""


def _journal_print_response(html_content, nom_fichier='journal'):
    """Applique le mode d'impression configuré aux journaux et fiches
    (règlements, comptabilité, paie, fiche client) qui renvoient du HTML.
      • mode 'apercu'  → HTML inchangé (comportement historique)
      • mode 'directe' → conversion PDF + envoi à l'imprimante configurée."""
    try:
        force_apercu = bool(request.args.get('apercu') or request.args.get('html')
                            or request.args.get('download'))
    except Exception:
        force_apercu = True
    if _impression_mode() != 'directe' or force_apercu:
        return html_content

    pdf_bytes, err_pdf = _html_to_pdf_bytes(html_content, _current_print_format())
    if not pdf_bytes:
        logging.warning("[PRINT] %s : PDF impossible (%s) — repli aperçu", nom_fichier, err_pdf)
        return html_content
    ok, printer_nom, err = _envoyer_pdf_imprimante_configuree(pdf_bytes)
    if ok:
        logging.info("[PRINT] %s → %s (direct)", nom_fichier, printer_nom)
    fb = None
    try:
        fb = request.full_path + ('&' if request.query_string else '') + 'apercu=1'
    except Exception:
        pass
    return _page_confirmation_impression(ok, printer_nom, err, fallback_url=fb)


def _document_pdf_response(html_content, doc_type, doc_id, filename=None):
    """Convertit le HTML d'un document (même gabarit / même design) en PDF et
    le renvoie directement au navigateur, qui l'ouvre dans sa visionneuse PDF
    intégrée — sans passer par la page HTML ni le dialog d'impression.

    Options via paramètres d'URL :
      • ?download=1  → force le téléchargement au lieu de l'ouverture inline
      • ?html=1      → revient à l'ancienne page HTML imprimable (secours)

    Repli automatique : si aucune bibliothèque PDF (weasyprint / pdfkit) n'est
    disponible, on retombe sur l'ancienne page HTML imprimable, sans planter.
    """
    # Échappatoire manuelle : ?html=1 → ancienne page HTML
    try:
        if request.args.get('html'):
            return _inject_direct_print_script(html_content, doc_type, doc_id)
    except Exception:
        pass

    fmt = _current_print_format()
    pdf_bytes, err = _html_to_pdf_bytes(html_content, fmt)
    if not pdf_bytes:
        # Aucune lib PDF dispo → on garde l'ancien comportement (page HTML)
        logging.warning("[PDF] %s #%s : conversion impossible (%s) — repli HTML",
                        doc_type, doc_id, err)
        return _inject_direct_print_script(html_content, doc_type, doc_id)

    # ── Mode 'directe' (Paramètres → Impression) ─────────────────────
    # Le document part directement à l'imprimante configurée, sans aperçu.
    # ?download=1 ou ?apercu=1 court-circuitent ce mode.
    if (_impression_mode() == 'directe'
            and not request.args.get('download')
            and not request.args.get('apercu')):
        ok_snd, printer_nom, err_snd = _envoyer_pdf_imprimante_configuree(pdf_bytes)
        if ok_snd:
            logging.info("[PRINT] %s #%s → %s (direct)", doc_type, doc_id, printer_nom)
        else:
            logging.warning("[PRINT] %s #%s : envoi direct impossible (%s)",
                            doc_type, doc_id, err_snd)
        fb = None
        try:
            fb = request.full_path + ('&' if request.query_string else '') + 'apercu=1'
        except Exception:
            pass
        return _page_confirmation_impression(ok_snd, printer_nom, err_snd, fallback_url=fb)

    # Nom de fichier propre
    if not filename:
        filename = f"{doc_type}_{doc_id}"
    filename = re.sub(r'[^A-Za-z0-9._-]+', '_', str(filename)).strip('_') or 'document'
    if not filename.lower().endswith('.pdf'):
        filename += '.pdf'

    disposition = 'attachment' if request.args.get('download') else 'inline'
    resp = app.response_class(pdf_bytes, mimetype='application/pdf')
    resp.headers['Content-Disposition'] = f"{disposition}; filename=\"{filename}\""
    resp.headers['Cache-Control']       = 'no-store'
    return resp


def _get_lan_ip():
    """Retourne l'adresse IP de la machine sur le réseau local (LAN),
    celle qu'un téléphone peut utiliser pour joindre l'application.
    Aucun paquet n'est réellement envoyé (UDP connect)."""
    import socket
    ip = '127.0.0.1'
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            ip = '127.0.0.1'
    finally:
        s.close()
    return ip


@app.route('/parametres', methods=['GET', 'POST'])
@login_required
def parametres():
    if session.get('user_role') != 'admin':
        flash("Accès réservé à l'administrateur.", "danger")
        return redirect(url_for('dashboard'))

    active_tab = request.args.get('tab', 'infos')

    # Gestion POST selon onglet
    if request.method == 'POST':
        onglet = request.form.get('onglet', 'infos')
        active_tab = onglet

        if onglet == 'infos':
            # Tous les champs de configuration générale
            champs = [
                'nom_depot','adresse','ville','telephone','email','rccm','tva',
                'devise','site_web','activite','prefixe_commande',
                'prefixe_facture_depot','prefixe_bl','prefixe_devis',
                'seuil_stock_alerte','slogan','ncc','pied_facture',
                'session_timeout',
                # ── Facture normalisée (FNE — DGI Côte d'Ivoire) ──
                'fne_url','cle_api_fne','fne_etablissement',
                'fne_point_vente','fne_vendeur','fne_mode',
                # ── Compta & TVA (fusionné depuis onglet article) ──
                'tva_deductible_taux','tva_deductible_compte','tva_deductible_libelle',
                'tva_collectee_taux','tva_collectee_compte','tva_collectee_libelle',
                # ── Format impression & imprimante ──
                'format_doc','imprimante_nom','impression_mode','impression_couleur',
                # ── Tiroir-caisse : nom Windows de l'imprimante thermique ──
                'thermique_nom',
            ]
            # Champs FNE : toujours écrire même si vide (pour permettre l'effacement)
            champs_fne = {'fne_url','cle_api_fne','fne_etablissement',
                          'fne_point_vente','fne_vendeur','fne_mode'}
            for cle in champs:
                val = request.form.get(cle, '')
                if val or cle in champs_fne:
                    execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)", (cle, val))

            # ── Tiroir-caisse (ESC/POS) : cases à cocher oui/non ──
            for _tk in ('tiroir_caisse', 'tiroir_auto'):
                execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)",
                        (_tk, 'oui' if request.form.get(_tk) == 'oui' else 'non'))

            # Checkboxes sécurité
            for cb in ['double_auth','lockout_active','log_connexions','auto_logout']:
                val = request.form.get(cb, 'non')
                execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)", (cb, val))
            # Checkboxes modules optionnels
            for cb_mod in ['module_caisse', 'module_emballages', 'module_atelier']:
                val_mod = 'oui' if request.form.get(cb_mod) == 'oui' else 'non'
                execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)", (cb_mod, val_mod))

            # ── Apparence : mode du menu (toggle → 'moderne' / 'classique') ──
            # Traité comme une case à cocher : décochée = on revient au mode classique.
            execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES('menu_mode',?)",
                    ('moderne' if request.form.get('menu_mode') == 'moderne' else 'classique',))

            # ── Logo des reçus (base64 DataURL) ──
            logo_recu_val = request.form.get('logo_recu', '').strip()
            if logo_recu_val == '__supprimer__':
                execute("DELETE FROM parametres WHERE cle='logo_recu'")
            elif logo_recu_val and logo_recu_val.startswith('data:image/'):
                execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)",
                        ('logo_recu', logo_recu_val))

            # ── Renommer la base de données selon le nom_depot ──
            _nom_depot_save = request.form.get('nom_depot', '').strip()
            if _nom_depot_save:
                try:
                    _nouveau_slug = _slugify_db(_nom_depot_save)
                    _nouveau_path = os.path.join(_DATA_DIR, _nouveau_slug + '.db')
                    if os.path.abspath(_nouveau_path) != os.path.abspath(DB_PATH):
                        # Fermer toutes les connexions ouvertes avant de renommer
                        if 'db' in g:
                            g.db.close()
                            g.pop('db', None)
                        os.rename(DB_PATH, _nouveau_path)
                        # Mettre à jour DB_PATH globalement pour les requêtes suivantes
                        import distrigest as _self
                        _self.DB_PATH = _nouveau_path
                        print(f"[INFO] Base renommée : {os.path.basename(DB_PATH)} → {os.path.basename(_nouveau_path)}")
                except Exception as _re:
                    print(f"[WARN] Renommage DB impossible : {_re}")

            flash("Paramètres enregistrés.", "success")

        elif onglet == 'acces':
            sub = request.form.get('sub_onglet', 'compte')
            action = request.form.get('user_action', '')

            # ── Nombre max de slots utilisateurs (hors admin) ──
            MAX_USER_SLOTS = 5

            def _set_param(k, v):
                execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)", (k, v))

            def _del_params_prefix(prefix):
                execute("DELETE FROM parametres WHERE cle LIKE ?", (prefix + '%',))

            def _find_free_slot():
                """Retourne le premier slot libre (user_1, user_2, ...) ou None si saturé."""
                rows = query("SELECT cle, valeur FROM parametres WHERE cle LIKE 'user_%_nom'")
                occupied = {r['cle'].split('_')[1] for r in rows if r['valeur']}
                for i in range(1, MAX_USER_SLOTS + 1):
                    if str(i) not in occupied:
                        return f'user_{i}'
                return None

            def _login_exists(login, exclude_slot=None):
                """Vérifie si un login est déjà pris dans les slots ou côté admin."""
                if not login:
                    return False
                cfg_admin = query("SELECT valeur FROM parametres WHERE cle='admin_username'", one=True)
                if cfg_admin and cfg_admin['valeur'] == login:
                    return True
                for i in range(1, MAX_USER_SLOTS + 1):
                    slot = f'user_{i}'
                    if slot == exclude_slot:
                        continue
                    r = query("SELECT valeur FROM parametres WHERE cle=?", (f'{slot}_login',), one=True)
                    if r and r['valeur'] == login:
                        return True
                return False

            def _sync_utilisateurs_slot(slot, nom, login, email, pwd, role, actif):
                """Crée ou met à jour la ligne correspondante dans la table `utilisateurs`
                   pour que le login fonctionne. Le slot est stocké dans `prenom` (réutilisation).
                   role peut être une chaîne CSV multi-rôles (ex: 'commercial,caissier') ;
                   on stocke le premier rôle comme rôle principal dans utilisateurs.role."""
                # Extraire le rôle principal (premier de la liste CSV)
                role_principal = role.split(',')[0].strip() if role else 'commercial'
                row = query("SELECT id FROM utilisateurs WHERE prenom=?", (slot,), one=True)
                if row:
                    if pwd:
                        execute("""UPDATE utilisateurs SET nom=?, username=?, email=?,
                                   mot_de_passe=?, role=?, actif=? WHERE id=?""",
                                (nom, login, email or None, pwd, role_principal, actif, row['id']))
                    else:
                        execute("""UPDATE utilisateurs SET nom=?, username=?, email=?,
                                   role=?, actif=? WHERE id=?""",
                                (nom, login, email or None, role_principal, actif, row['id']))
                else:
                    execute("""INSERT INTO utilisateurs(nom, prenom, username, email,
                               mot_de_passe, role, actif)
                               VALUES(?,?,?,?,?,?,?)""",
                            (nom, slot, login, email or None, pwd or '', role_principal, actif))

            # ════════════════════════════════════════════════════════════
            #  ACTION : CRÉER un nouvel utilisateur
            # ════════════════════════════════════════════════════════════
            if action == 'add':
                nom    = request.form.get('u_nom','').strip()
                login_ = request.form.get('u_login','').strip()
                mail   = request.form.get('u_email','').strip()
                pwd    = request.form.get('u_password','').strip()
                role_raw = request.form.get('u_role','commercial')
                # Normaliser rôles CSV : strip + minuscule sur chaque entrée
                role   = ','.join(r.strip().lower() for r in role_raw.split(',') if r.strip()) or 'commercial'
                statut = request.form.get('u_statut','actif')
                actif  = 1 if statut == 'actif' else 0

                if not (nom and login_ and pwd):
                    flash("Nom, identifiant et mot de passe sont obligatoires.", "danger")
                    return redirect(url_for('parametres', tab='acces'))

                if _login_exists(login_):
                    flash(f"L'identifiant « {login_} » est déjà utilisé.", "danger")
                    return redirect(url_for('parametres', tab='acces'))

                slot = _find_free_slot()
                if not slot:
                    flash(f"Limite atteinte : {MAX_USER_SLOTS} utilisateurs maximum.", "danger")
                    return redirect(url_for('parametres', tab='acces'))

                # Écrire dans la table parametres (slots) — c'est ce que le template affiche
                _set_param(f'{slot}_nom',      nom.upper())
                _set_param(f'{slot}_login',    login_)
                _set_param(f'{slot}_email',    mail)
                _set_param(f'{slot}_password', pwd)
                _set_param(f'{slot}_role',     role)
                _set_param(f'{slot}_statut',   statut)

                # Synchroniser table utilisateurs pour permettre le login
                _sync_utilisateurs_slot(slot, nom.upper(), login_, mail, pwd, role, actif)

                flash(f"✅ Utilisateur « {nom.upper()} » créé avec succès.", "success")

            # ════════════════════════════════════════════════════════════
            #  ACTION : MODIFIER un utilisateur existant
            # ════════════════════════════════════════════════════════════
            elif action == 'edit':
                slot = request.form.get('user_slot','').strip()
                if not slot or not slot.startswith('user_'):
                    flash("Utilisateur introuvable.", "danger")
                    return redirect(url_for('parametres', tab='acces'))

                nom    = request.form.get('u_nom','').strip()
                login_ = request.form.get('u_login','').strip()
                mail   = request.form.get('u_email','').strip()
                pwd    = request.form.get('u_password','').strip()  # vide = inchangé
                role_raw = request.form.get('u_role','commercial')
                # Normaliser rôles CSV : strip + minuscule sur chaque entrée
                role   = ','.join(r.strip().lower() for r in role_raw.split(',') if r.strip()) or 'commercial'
                statut = request.form.get('u_statut','actif')
                actif  = 1 if statut == 'actif' else 0

                if not (nom and login_):
                    flash("Nom et identifiant sont obligatoires.", "danger")
                    return redirect(url_for('parametres', tab='acces'))

                if _login_exists(login_, exclude_slot=slot):
                    flash(f"L'identifiant « {login_} » est déjà utilisé.", "danger")
                    return redirect(url_for('parametres', tab='acces'))

                # Lire l'ancien mot de passe si pas modifié
                if not pwd:
                    old = query("SELECT valeur FROM parametres WHERE cle=?",
                                (f'{slot}_password',), one=True)
                    pwd = old['valeur'] if old else ''

                _set_param(f'{slot}_nom',      nom.upper())
                _set_param(f'{slot}_login',    login_)
                _set_param(f'{slot}_email',    mail)
                _set_param(f'{slot}_password', pwd)
                _set_param(f'{slot}_role',     role)
                _set_param(f'{slot}_statut',   statut)

                _sync_utilisateurs_slot(slot, nom.upper(), login_, mail, pwd, role, actif)

                flash(f"✅ Utilisateur « {nom.upper()} » mis à jour.", "success")

            # ════════════════════════════════════════════════════════════
            #  ACTION : SUPPRIMER un utilisateur
            # ════════════════════════════════════════════════════════════
            elif action == 'delete':
                slot = request.form.get('user_slot','').strip()
                if slot and slot.startswith('user_'):
                    # Supprimer toutes les clés parametres liées (infos + permissions)
                    _del_params_prefix(slot + '_')
                    # Supprimer la ligne synchronisée dans utilisateurs
                    execute("DELETE FROM utilisateurs WHERE prenom=? AND role != 'admin'", (slot,))
                    flash("🗑️ Utilisateur supprimé.", "success")
                else:
                    flash("Suppression impossible.", "danger")

            # ════════════════════════════════════════════════════════════
            #  ACTION : MODIFIER LE COMPTE ADMIN (depuis la modale)
            # ════════════════════════════════════════════════════════════
            elif action == 'edit_admin':
                nom    = request.form.get('u_nom','').strip()
                login_ = request.form.get('u_login','').strip()
                mail   = request.form.get('u_email','').strip()
                pwd    = request.form.get('u_password','').strip()  # vide = inchangé

                if not (nom and login_):
                    flash("Nom et identifiant admin sont obligatoires.", "danger")
                    return redirect(url_for('parametres', tab='acces'))

                _set_param('admin_nom',      nom)
                _set_param('admin_username', login_)
                _set_param('admin_email',    mail)

                if pwd:
                    execute("UPDATE utilisateurs SET nom=?, username=?, email=?, mot_de_passe=? WHERE role='admin'",
                            (nom, login_, mail or None, pwd))
                    flash("✅ Compte admin mis à jour (mot de passe modifié).", "success")
                else:
                    execute("UPDATE utilisateurs SET nom=?, username=?, email=? WHERE role='admin'",
                            (nom, login_, mail or None))
                    flash("✅ Compte admin mis à jour.", "success")

            # ════════════════════════════════════════════════════════════
            #  Anciens onglets : compte admin via formulaire long
            # ════════════════════════════════════════════════════════════
            elif sub == 'compte':
                # ── Modifier le compte administrateur principal (formulaire complet) ──
                nom  = request.form.get('admin_nom','')
                usr  = request.form.get('admin_username','')
                mail = request.form.get('admin_email','')
                tel  = request.form.get('admin_telephone','')
                pwd  = request.form.get('admin_mot_de_passe','')
                conf = request.form.get('admin_mot_de_passe_confirm','')
                poste = request.form.get('admin_poste','gerant')
                for cle, val in [('admin_nom',nom),('admin_username',usr),('admin_email',mail),
                                  ('admin_telephone',tel),('admin_poste',poste)]:
                    _set_param(cle, val)
                if usr:
                    execute("UPDATE utilisateurs SET nom=?, username=? WHERE role='admin'",
                            (nom or 'Admin', usr))
                if pwd and pwd == conf:
                    execute("UPDATE utilisateurs SET mot_de_passe=? WHERE role='admin'", (pwd,))
                    flash("Mot de passe mis à jour.", "success")
                elif pwd and pwd != conf:
                    flash("Les mots de passe ne correspondent pas.", "danger")
                    return redirect(url_for('parametres', tab='acces'))
                flash("Compte administrateur mis à jour.", "success")

            elif sub == 'fonctions':
                slot = request.form.get('target_user_slot','')
                if slot == 'admin':
                    flash("L'administrateur a tous les droits — ses permissions ne peuvent pas être modifiées.", "info")
                elif slot:
                    for mod in [
                                'acces_caisse',
                                'acces_atelier_equipements','acces_atelier_tickets',
                                'acces_devis','acces_commandes','acces_factures','acces_avoirs',
                                'acces_achats','acces_factures_fourn',
                                'acces_clients','acces_fournisseurs','acces_relances','acces_representants','acces_emballages',
                                'acces_reglements','acces_depenses','acces_comptabilite',
                                'acces_articles','acces_familles','acces_unites','acces_stock','acces_depots',
                                'acces_employes','acces_paie','acces_conges']:
                        raw = request.form.get(mod, 'non')
                        # Normaliser : oui → ecriture (rétrocompat), valeurs valides seulement
                        if raw == 'oui':
                            raw = 'ecriture'
                        if raw not in ('non', 'lecture', 'ecriture'):
                            raw = 'non'
                        execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)",
                                (slot + '_' + mod, raw))
                    flash("Autorisations mises à jour.", "success")

        elif onglet == 'reseau':
            # Champs texte / numériques — enregistrer même vides (permet d'effacer)
            for cle in ['server_host', 'server_port', 'backup_heure', 'backup_dossier']:
                val = request.form.get(cle, '').strip()
                execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)", (cle, val))
            # Champs booléens (checkbox) — oui / non
            for cle in ['backup_auto', 'mode_debug', 'log_acces']:
                val = 'oui' if request.form.get(cle) == 'oui' else 'non'
                execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)", (cle, val))
            flash("Paramètres réseau enregistrés.", "success")

        elif onglet == 'article':
            # Onglet fusionné dans 'infos' — on redirige proprement
            active_tab = 'infos'
            flash("Paramètres TVA enregistrés.", "success")

        elif onglet == 'imprimante':
            for cle in ['imprimante_type','imprimante_nom','imprimante_copies','imprimante_marge']:
                val = request.form.get(cle, '')
                execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)", (cle, val))
            for cle in ['imprimante_facture_auto','imprimante_bl_auto','imprimante_recu_auto',
                        'imprimante_achat_auto','impression_entete','impression_pied_page',
                        'impression_preview','impression_signatures']:
                val = 'oui' if request.form.get(cle) == 'oui' else 'non'
                execute("INSERT OR REPLACE INTO parametres(cle,valeur) VALUES(?,?)", (cle, val))
            flash("Paramètres imprimante enregistrés.", "success")

        elif onglet == 'notifications':
            return redirect(url_for('notif_save'))

        return redirect(url_for('parametres', tab=active_tab))

    # GET — construire config complète
    rows = query("SELECT cle, valeur FROM parametres")
    config = {r['cle']: r['valeur'] for r in rows}
    for _k, _v in {'imprimante_type':'a4','imprimante_nom':'','imprimante_copies':'1',
                   'imprimante_marge':'normale','imprimante_facture_auto':'non',
                   'imprimante_bl_auto':'non','imprimante_recu_auto':'non',
                   'imprimante_achat_auto':'non','impression_entete':'oui',
                   'impression_pied_page':'oui','impression_preview':'oui',
                   'impression_signatures':'oui',
                   'tiroir_caisse':'non','tiroir_auto':'oui'}.items():
        config.setdefault(_k, _v)

    utilisateurs = query("SELECT * FROM utilisateurs ORDER BY nom")

    # Accès par utilisateur pour le JS
    user_acces = {}
    all_mods = [
                'acces_caisse',
                'acces_atelier_equipements','acces_atelier_tickets',
                'acces_devis','acces_commandes','acces_factures','acces_avoirs',
                'acces_achats','acces_factures_fourn',
                'acces_clients','acces_fournisseurs','acces_relances','acces_representants','acces_emballages',
                'acces_reglements','acces_depenses','acces_comptabilite',
                'acces_articles','acces_familles','acces_unites','acces_stock','acces_depots',
                'acces_employes','acces_paie','acces_conges'
    ]
    for u in utilisateurs:
        slot = u['nom'] if u['nom'] else ''
        if slot:
            def _norm(v): return 'ecriture' if v == 'oui' else v
            user_acces[slot] = {mod: _norm(config.get(slot+'_'+mod,'ecriture')) for mod in all_mods}

    return render_template('parametres.html',
                           cfg=config, config=config,
                           utilisateurs=utilisateurs,
                           user_acces=user_acces,
                           active_tab=active_tab,
                           lan_ip=_get_lan_ip(),
                           lan_url="http://%s:%s" % (_get_lan_ip(), (config.get('server_port') or '1439')),
                           session_user_id=session.get('user_id'))

@app.route('/parametres/backup')
@login_required
def parametres_backup():
    if session.get('user_role') != 'admin':
        return redirect(url_for('dashboard'))
    import os, shutil
    db_path = DB_PATH
    backup_name = f"distrigest_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    from flask import send_file
    return send_file(db_path, as_attachment=True, download_name=backup_name)

@app.route('/utilisateurs/add', methods=['POST'])
@login_required
def utilisateur_add():
    if session.get('user_role') != 'admin':
        flash("Non autorisé.", "danger")
        return redirect(url_for('parametres'))
    f = request.form
    pwd = f.get('password') or f.get('mot_de_passe', '')
    username = f.get('username') or f.get('email', '')
    execute("""INSERT OR IGNORE INTO utilisateurs(nom, username, mot_de_passe, role, actif)
               VALUES(?,?,?,?,1)""",
            (f['nom'], username, pwd, f.get('role', 'operateur')))
    flash("Utilisateur créé.", "success")
    return redirect(url_for('parametres'))

@app.route('/utilisateurs/delete/<int:id>')
@login_required
def utilisateur_delete(id):
    if session.get('user_role') != 'admin':
        return redirect(url_for('dashboard'))
    execute("UPDATE utilisateurs SET actif=0 WHERE id=?", (id,))
    flash("Utilisateur désactivé.", "success")
    return redirect(url_for('parametres'))

# ══════════════════════════════════════════════════════════════════════
#  RECHERCHE GLOBALE
# ══════════════════════════════════════════════════════════════════════
@app.route('/search')
@login_required
def search():
    cfg = get_cfg()
    q = request.args.get('q', '').strip()
    results = {}
    total = 0
    if q:
        like = f'%{q}%'
        results['articles'] = query(
            "SELECT * FROM articles WHERE actif=1 AND (designation LIKE ? OR reference LIKE ?) LIMIT 10",
            (like, like))
        results['clients'] = query(
            "SELECT * FROM clients WHERE actif=1 AND (nom LIKE ? OR prenom LIKE ? OR telephone LIKE ?) LIMIT 10",
            (like, like, like))
        results['factures'] = query(
            """SELECT dv.*, c.nom as client_nom FROM documents_vente dv
               LEFT JOIN clients c ON c.id=dv.client_id
               WHERE dv.type_doc='facture' AND (dv.reference LIKE ? OR c.nom LIKE ?) LIMIT 10""",
            (like, like))
        results['fournisseurs'] = query(
            "SELECT * FROM fournisseurs WHERE actif=1 AND (nom LIKE ? OR contact LIKE ?) LIMIT 10",
            (like, like))
        results['stocks'] = query(
            """SELECT a.designation, a.famille, a.colisage, s.quantite_unite, s.quantite_colis,
                      s.stock_min_unite as seuil_alerte, d.nom as depot_nom
               FROM stocks s JOIN articles a ON a.id=s.article_id
               JOIN depots d ON d.id=s.depot_id
               WHERE a.actif=1 AND (a.designation LIKE ? OR a.reference LIKE ?) LIMIT 10""",
            (like, like))
        results['employes'] = query(
            "SELECT * FROM employes WHERE actif=1 AND (nom LIKE ? OR prenom LIKE ? OR poste LIKE ?) LIMIT 10",
            (like, like, like))
        results['depenses'] = query(
            "SELECT * FROM depenses WHERE description LIKE ? OR categorie LIKE ? ORDER BY date_depense DESC LIMIT 10",
            (like, like))
        total = sum(len(v) for v in results.values())
    return render_template('search.html', cfg=cfg, q=q, results=results, total=total)

@app.route('/api/search')
@login_required
def api_search():
    q = request.args.get('q','').strip()
    if len(q) < 2:
        return jsonify({'results': []})
    like = f'%{q}%'
    results = []
    for row in query("SELECT id,reference,designation FROM articles WHERE actif=1 AND (designation LIKE ? OR reference LIKE ?) LIMIT 5", (like, like)):
        results.append({'type':'article','icon':'🛒','label':row['designation'],'sub':row['reference'],'url':url_for('articles_list'),'statut':None})
    for row in query("SELECT id,nom,prenom,telephone FROM clients WHERE actif=1 AND (nom LIKE ? OR telephone LIKE ?) LIMIT 5", (like, like)):
        results.append({'type':'client','icon':'👤','label':f"{row['nom']} {row['prenom'] or ''}".strip(),'sub':row['telephone'] or '','url':url_for('clients_list'),'statut':None})
    for row in query("SELECT id,reference,total_ttc,statut FROM documents_vente WHERE type_doc='facture' AND reference LIKE ? LIMIT 5", (like,)):
        results.append({'type':'facture','icon':'🧾','label':row['reference'],'sub':f"{fcfa(row['total_ttc'])} FCFA",'url':url_for('factures_list'),'statut':row['statut']})
    return jsonify({'results': results})


# ══════════════════════════════════════════════════════════════════════
#  API ALERTES — Centre de notifications base de données
#  GET /api/alertes  → liste toutes les alertes actives
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/alertes')
@login_required
def api_alertes():
    """Retourne toutes les alertes métier issues de la base de données.

    Chaque alerte : { id, cat, icon, msg, url, ts }
      cat  : 'danger' | 'warning' | 'info' | 'success'
      ts   : timestamp ISO (date de référence de l'événement)
    """
    today      = date.today()
    today_iso  = today.isoformat()
    alertes    = []

    # ── 1. STOCK : ruptures (qté = 0) ──────────────────────────────
    ruptures = query("""
        SELECT a.designation, a.reference, d.nom as depot,
               s.quantite_unite
        FROM stocks s
        JOIN articles a ON a.id = s.article_id
        JOIN depots   d ON d.id = s.depot_id
        WHERE s.quantite_unite <= 0 AND a.actif = 1
        ORDER BY a.designation
        LIMIT 20
    """)
    for r in ruptures:
        alertes.append({
            'id'  : f"rupture_{r['reference']}_{r['depot']}",
            'cat' : 'danger',
            'icon': '📦',
            'msg' : f"<strong>Rupture de stock</strong> — {r['designation']} "
                    f"({r['reference']}) · Dépôt {r['depot']}",
            'url' : url_for('stock_list'),
            'ts'  : today_iso,
        })

    # ── 2. STOCK : seuil d'alerte (qté ≤ stock_min) ────────────────
    bas = query("""
        SELECT a.designation, a.reference, d.nom as depot,
               s.quantite_unite, s.stock_min_unite
        FROM stocks s
        JOIN articles a ON a.id = s.article_id
        JOIN depots   d ON d.id = s.depot_id
        WHERE s.quantite_unite > 0
          AND s.quantite_unite <= s.stock_min_unite
          AND s.stock_min_unite > 0
          AND a.actif = 1
        ORDER BY (s.quantite_unite - s.stock_min_unite) ASC
        LIMIT 20
    """)
    for r in bas:
        alertes.append({
            'id'  : f"stock_bas_{r['reference']}_{r['depot']}",
            'cat' : 'warning',
            'icon': '⚠️',
            'msg' : f"<strong>Stock faible</strong> — {r['designation']} : "
                    f"{r['quantite_unite']} unité(s) (seuil {r['stock_min_unite']}) · "
                    f"Dépôt {r['depot']}",
            'url' : url_for('stock_list'),
            'ts'  : today_iso,
        })

    # ── 3. ARTICLES : date de péremption dépassée ou proche (30 j) ─
    try:
        limit_peri = (today + timedelta(days=30)).isoformat()
        perimes = query("""
            SELECT designation, reference, date_peremption
            FROM articles
            WHERE actif = 1
              AND date_peremption IS NOT NULL AND date_peremption != ''
              AND date_peremption <= ?
            ORDER BY date_peremption ASC
            LIMIT 20
        """, (limit_peri,))
        for r in perimes:
            depasse = r['date_peremption'] < today_iso
            alertes.append({
                'id'  : f"peremption_{r['reference']}",
                'cat' : 'danger' if depasse else 'warning',
                'icon': '🧪',
                'msg' : ("<strong>Périmé</strong>" if depasse else "<strong>Péremption proche</strong>") +
                        f" — {r['designation']} ({r['reference']}) · {r['date_peremption']}",
                'url' : url_for('articles_list'),
                'ts'  : r['date_peremption'],
            })
    except Exception:
        pass

    # ── 4. FACTURES : échéances dépassées (impayées) ───────────────
    fact_retard = query("""
        SELECT dv.reference, dv.reste, dv.date_echeance, c.nom as client_nom
        FROM documents_vente dv
        LEFT JOIN clients c ON c.id = dv.client_id
        WHERE dv.type_doc   = 'facture'
          AND dv.statut     IN ('en_attente', 'partielle')
          AND dv.reste      > 0
          AND dv.date_echeance IS NOT NULL
          AND dv.date_echeance < ?
        ORDER BY dv.date_echeance ASC
        LIMIT 20
    """, (today_iso,))
    for r in fact_retard:
        jours = (today - date.fromisoformat(r['date_echeance'])).days
        alertes.append({
            'id'  : f"facture_retard_{r['reference']}",
            'cat' : 'danger',
            'icon': '🧾',
            'msg' : f"<strong>Facture en retard</strong> — {r['reference']} "
                    f"({r['client_nom'] or 'Client inconnu'}) · "
                    f"{jours}j de retard · Reste {fcfa(r['reste'])} FCFA",
            'url' : url_for('factures_list'),
            'ts'  : r['date_echeance'],
        })

    # ── 5. FACTURES : échéances dans les 7 jours ──────────────────
    limit_fact = (today + timedelta(days=7)).isoformat()
    fact_proche = query("""
        SELECT dv.reference, dv.reste, dv.date_echeance, c.nom as client_nom
        FROM documents_vente dv
        LEFT JOIN clients c ON c.id = dv.client_id
        WHERE dv.type_doc   = 'facture'
          AND dv.statut     IN ('en_attente', 'partielle')
          AND dv.reste      > 0
          AND dv.date_echeance IS NOT NULL
          AND dv.date_echeance >= ?
          AND dv.date_echeance <= ?
        ORDER BY dv.date_echeance ASC
        LIMIT 10
    """, (today_iso, limit_fact))
    for r in fact_proche:
        jours = (date.fromisoformat(r['date_echeance']) - today).days
        alertes.append({
            'id'  : f"facture_proche_{r['reference']}",
            'cat' : 'warning',
            'icon': '📅',
            'msg' : f"<strong>Échéance imminente</strong> — {r['reference']} "
                    f"({r['client_nom'] or 'Client inconnu'}) dans {jours}j · "
                    f"Reste {fcfa(r['reste'])} FCFA",
            'url' : url_for('factures_list'),
            'ts'  : r['date_echeance'],
        })

    # ── 6. COMMANDES VENTE : en attente depuis > 5 jours ──────────
    limit_cmde = (today - timedelta(days=5)).isoformat()
    cmdes_att = query("""
        SELECT dv.reference, dv.date_doc, c.nom as client_nom
        FROM documents_vente dv
        LEFT JOIN clients c ON c.id = dv.client_id
        WHERE dv.type_doc = 'commande'
          AND dv.statut   IN ('en_attente', 'confirme')
          AND dv.date_doc < ?
        ORDER BY dv.date_doc ASC
        LIMIT 10
    """, (limit_cmde,))
    for r in cmdes_att:
        jours = (today - date.fromisoformat(r['date_doc'])).days
        alertes.append({
            'id'  : f"cmde_att_{r['reference']}",
            'cat' : 'warning',
            'icon': '🗂️',
            'msg' : f"<strong>Commande non traitée</strong> — {r['reference']} "
                    f"({r['client_nom'] or '?'}) · {jours}j sans mise à jour",
            'url' : url_for('commandes_vente_list'),
            'ts'  : r['date_doc'],
        })

    # ── 7. BONS DE LIVRAISON : date prévue dépassée ───────────────
    try:
        bl_retard = query("""
            SELECT dv.reference, dv.date_livraison_prevue, c.nom as client_nom
            FROM documents_vente dv
            LEFT JOIN clients c ON c.id = dv.client_id
            WHERE dv.type_doc              = 'bon_livraison'
              AND dv.statut               NOT IN ('livre', 'facture', 'annule')
              AND dv.date_livraison_prevue IS NOT NULL
              AND dv.date_livraison_prevue < ?
            ORDER BY dv.date_livraison_prevue ASC
            LIMIT 10
        """, (today_iso,))
        for r in bl_retard:
            jours = (today - date.fromisoformat(r['date_livraison_prevue'])).days
            alertes.append({
                'id'  : f"bl_retard_{r['reference']}",
                'cat' : 'danger',
                'icon': '🚚',
                'msg' : f"<strong>Livraison en retard</strong> — {r['reference']} "
                        f"({r['client_nom'] or '?'}) · {jours}j de retard",
                'url' : url_for('bons_livraison_list'),
                'ts'  : r['date_livraison_prevue'],
            })
    except Exception:
        pass

    # ── 8. RELANCES : planifiées dont la date est dépassée ─────────
    try:
        relances_dues = query("""
            SELECT r.reference, r.date_echeance, c.nom as client_nom, r.montant_du
            FROM relances r
            LEFT JOIN clients c ON c.id = r.client_id
            WHERE r.statut IN ('planifiee', 'envoyee')
              AND r.date_echeance IS NOT NULL
              AND r.date_echeance < ?
            ORDER BY r.date_echeance ASC
            LIMIT 10
        """, (today_iso,))
        for r in relances_dues:
            alertes.append({
                'id'  : f"relance_{r['reference']}",
                'cat' : 'warning',
                'icon': '🔔',
                'msg' : f"<strong>Relance en attente</strong> — {r['client_nom'] or '?'} "
                        f"· Montant dû {fcfa(r['montant_du'] or 0)} FCFA",
                'url' : url_for('relances_list'),
                'ts'  : r['date_echeance'],
            })
    except Exception:
        pass

    # ── 9. CONGÉS : en attente de validation ──────────────────────
    try:
        conges_pend = query("""
            SELECT cg.id, cg.date_debut, cg.date_fin, cg.nb_jours,
                   e.nom as employe_nom, e.prenom as employe_prenom
            FROM conges cg
            JOIN employes e ON e.id = cg.employe_id
            WHERE cg.statut = 'en_attente'
            ORDER BY cg.date_debut ASC
            LIMIT 10
        """)
        for r in conges_pend:
            alertes.append({
                'id'  : f"conge_{r['id']}",
                'cat' : 'info',
                'icon': '🏖️',
                'msg' : f"<strong>Congé à valider</strong> — "
                        f"{r['employe_nom']} {r['employe_prenom'] or ''} "
                        f"({r['nb_jours']} j) du {r['date_debut']} au {r['date_fin']}",
                'url' : url_for('conges_list'),
                'ts'  : r['date_debut'],
            })
    except Exception:
        pass

    # ── 10. EMBALLAGES CONSIGNÉS : retard de retour ────────────────
    try:
        emb_retard = query("""
            SELECT c.id, c.date_retour_prevu, c.quantite,
                   cl.nom as client_nom, t.nom as type_nom
            FROM consignes c
            JOIN clients cl          ON cl.id = c.client_id
            JOIN types_emballages t  ON t.id  = c.type_emballage_id
            WHERE c.statut = 'en_cours'
              AND c.date_retour_prevu IS NOT NULL
              AND c.date_retour_prevu < ?
            ORDER BY c.date_retour_prevu ASC
            LIMIT 10
        """, (today_iso,))
        for r in emb_retard:
            jours = (today - date.fromisoformat(r['date_retour_prevu'])).days
            alertes.append({
                'id'  : f"emb_{r['id']}",
                'cat' : 'warning',
                'icon': '📫',
                'msg' : f"<strong>Consigne non retournée</strong> — "
                        f"{r['quantite']}× {r['type_nom']} "
                        f"({r['client_nom']}) · {jours}j de retard",
                'url' : url_for('emballages'),
                'ts'  : r['date_retour_prevu'],
            })
    except Exception:
        pass


    # ── 11. FACTURES VENTE : non réglées/partielles — relance tous les 5 j ─
    try:
        fact_vente_impayes = query("""
            SELECT dv.id, dv.reference, dv.reste, dv.date_doc,
                   c.nom as client_nom
            FROM documents_vente dv
            LEFT JOIN clients c ON c.id = dv.client_id
            WHERE dv.type_doc = 'facture'
              AND dv.statut   IN ('en_attente', 'partielle')
              AND dv.reste    > 0
              AND dv.date_doc IS NOT NULL
            ORDER BY dv.date_doc ASC
        """)
        for r in fact_vente_impayes:
            try:
                d0   = date.fromisoformat(r['date_doc'])
                jours = (today - d0).days
                # Notifier uniquement si multiple de 5 jours (et au moins 5j)
                if jours >= 5 and jours % 5 == 0:
                    cat = 'danger' if jours >= 15 else 'warning'
                    alertes.append({
                        'id'  : f"fact_vente_impaye_{r['reference']}_j{jours}",
                        'cat' : cat,
                        'icon': '🧾',
                        'msg' : f"<strong>Facture vente impayée</strong> — {r['reference']} "
                                f"({r['client_nom'] or '?'}) · "
                                f"{jours}j depuis émission · Reste {fcfa(r['reste'])} FCFA",
                        'url' : url_for('factures_list'),
                        'ts'  : r['date_doc'],
                    })
            except Exception:
                pass
    except Exception:
        pass

    # ── 12. FACTURES ACHAT : non réglées/partielles — relance tous les 5 j ─
    try:
        fact_achat_impayes = query("""
            SELECT ff.id, ff.reference, ff.reste, ff.date_facture,
                   f.nom as fournisseur_nom
            FROM factures_fournisseurs ff
            LEFT JOIN fournisseurs f ON f.id = ff.fournisseur_id
            WHERE ff.statut IN ('en_attente', 'partielle')
              AND ff.reste  > 0
              AND ff.date_facture IS NOT NULL
            ORDER BY ff.date_facture ASC
        """)
        for r in fact_achat_impayes:
            try:
                d0    = date.fromisoformat(r['date_facture'])
                jours = (today - d0).days
                if jours >= 5 and jours % 5 == 0:
                    cat = 'danger' if jours >= 15 else 'warning'
                    alertes.append({
                        'id'  : f"fact_achat_impaye_{r['reference']}_j{jours}",
                        'cat' : cat,
                        'icon': '🏭',
                        'msg' : f"<strong>Facture achat impayée</strong> — {r['reference']} "
                                f"({r['fournisseur_nom'] or '?'}) · "
                                f"{jours}j depuis réception · Reste {fcfa(r['reste'])} FCFA",
                        'url' : url_for('factures_fournisseurs_list'),
                        'ts'  : r['date_facture'],
                    })
            except Exception:
                pass
    except Exception:
        pass

    # ── 13. RELANCES : planifiées/envoyées — rappel tous les 5 j ──────────
    try:
        relances_actives = query("""
            SELECT rl.id, rl.date_relance, rl.montant_du,
                   c.nom as client_nom
            FROM relances rl
            LEFT JOIN clients c ON c.id = rl.client_id
            WHERE rl.statut IN ('planifiee', 'envoyee')
              AND rl.date_relance IS NOT NULL
            ORDER BY rl.date_relance ASC
        """)
        for r in relances_actives:
            try:
                d0    = date.fromisoformat(r['date_relance'])
                jours = (today - d0).days
                if jours >= 5 and jours % 5 == 0:
                    alertes.append({
                        'id'  : f"relance_actif_{r['id']}_j{jours}",
                        'cat' : 'warning',
                        'icon': '🔔',
                        'msg' : f"<strong>Relance sans réponse</strong> — "
                                f"{r['client_nom'] or '?'} · "
                                f"{jours}j sans retour · Dû {fcfa(r['montant_du'] or 0)} FCFA",
                        'url' : url_for('relances_list'),
                        'ts'  : r['date_relance'],
                    })
            except Exception:
                pass
    except Exception:
        pass


    # Trier : danger en premier, puis warning, puis info ; par date ASC
    _ordre = {'danger': 0, 'warning': 1, 'info': 2, 'success': 3}
    alertes.sort(key=lambda x: (_ordre.get(x['cat'], 9), x['ts']))

    return jsonify({'alertes': alertes, 'total': len(alertes)})


# ══════════════════════════════════════════════════════════════════════
#  REPRESENTANTS & COMMISSIONS
# ══════════════════════════════════════════════════════════════════════
@app.route('/representants')
@login_required
def representants_list():
    cfg = get_cfg()
    reps = query("""
        SELECT r.*,
          COUNT(DISTINCT c.id) as nb_clients,
          COALESCE(SUM(CASE WHEN cm.statut='en_attente' THEN cm.montant_commission ELSE 0 END),0) as commissions_dues,
          COALESCE(SUM(cm.montant_commission),0) as commissions_total
        FROM representants r
        LEFT JOIN clients c ON c.representant_id=r.id AND c.actif=1
        LEFT JOIN commissions cm ON cm.representant_id=r.id
        WHERE r.actif=1
        GROUP BY r.id ORDER BY r.nom
    """)
    # KPIs agrégés
    kpi = query("""SELECT
        COUNT(DISTINCT r.id)                                                   as nb_reps,
        COALESCE(SUM(CASE WHEN cm.statut='en_attente' THEN cm.montant_commission ELSE 0 END),0) as total_dues,
        COALESCE(SUM(cm.montant_commission),0)                                 as total_commissions,
        COUNT(DISTINCT c.id)                                                   as total_clients
        FROM representants r
        LEFT JOIN clients c ON c.representant_id=r.id AND c.actif=1
        LEFT JOIN commissions cm ON cm.representant_id=r.id
        WHERE r.actif=1""", one=True)
    return render_template('representants.html', cfg=cfg, reps=reps,
                           nb_reps=kpi['nb_reps'],
                           total_dues=kpi['total_dues'],
                           total_commissions=kpi['total_commissions'],
                           total_clients=kpi['total_clients'])

@app.route('/representants/add', methods=['POST'])
@login_required
def representant_add():
    f = request.form
    n = query("SELECT COUNT(*) as c FROM representants", one=True)['c']
    code = f"REP{n+1:04d}"
    execute("""INSERT INTO representants(code,nom,prenom,telephone,email,zone,taux_commission,notes)
               VALUES(?,?,?,?,?,?,?,?)""",
            (code, f['nom'].upper(), f.get('prenom'), f.get('telephone'),
             f.get('email'), f.get('zone'), float(f.get('taux_commission',5) or 5),
             f.get('notes')))
    flash("Représentant ajouté.", "success")
    return redirect(url_for('representants_list'))

@app.route('/representants/edit/<int:id>', methods=['POST'])
@login_required
def representant_edit(id):
    f = request.form
    execute("""UPDATE representants SET nom=?,prenom=?,telephone=?,email=?,zone=?,taux_commission=?,notes=?
               WHERE id=?""",
            (f['nom'].upper(), f.get('prenom'), f.get('telephone'), f.get('email'),
             f.get('zone'), float(f.get('taux_commission',5) or 5), f.get('notes'), id))
    flash("Représentant modifié.", "success")
    return redirect(url_for('representants_list'))

@app.route('/representants/delete/<int:id>')
@login_required
def representant_delete(id):
    execute("UPDATE representants SET actif=0 WHERE id=?", (id,))
    flash("Représentant désactivé.", "success")
    return redirect(url_for('representants_list'))

@app.route('/commissions/payer/<int:id>', methods=['POST'])
@login_required
def commission_payer(id):
    execute("UPDATE commissions SET statut='payee', date_paiement=date('now') WHERE id=?", (id,))
    flash("Commission marquée comme payée.", "success")
    return redirect(request.referrer or url_for('representants_list'))

# ══════════════════════════════════════════════════════════════════════
#  RELANCES
# ══════════════════════════════════════════════════════════════════════
@app.route('/relances')
@login_required
def relances_list():
    cfg = get_cfg()
    statut_f = request.args.get('statut','')
    sql = """SELECT r.*, c.nom as client_nom, c.telephone as client_tel,
             dv.reference as facture_ref, dv.total_ttc as facture_ttc
             FROM relances r
             LEFT JOIN clients c ON c.id=r.client_id
             LEFT JOIN documents_vente dv ON dv.id=r.facture_id
             WHERE 1=1"""
    args = []
    if statut_f:
        sql += " AND r.statut=?"; args.append(statut_f)
    sql += " ORDER BY r.date_relance DESC"
    relances = query(sql, args)
    # Clients avec impayés pour modal add
    clients_dus = query("""
        SELECT c.id, c.nom, c.telephone,
               COALESCE(SUM(dv.reste),0) as total_du,
               COUNT(dv.id) as nb_impayees
        FROM clients c
        JOIN documents_vente dv ON dv.client_id=c.id AND dv.type_doc='facture'
            AND dv.statut IN ('en_attente','partielle') AND dv.reste > 0
        WHERE c.actif=1
        GROUP BY c.id HAVING total_du > 0
        ORDER BY total_du DESC
    """)
    factures_dues = query("""
        SELECT dv.id, dv.reference, dv.reste, dv.date_echeance, c.nom as client_nom
        FROM documents_vente dv
        JOIN clients c ON c.id=dv.client_id
        WHERE dv.type_doc='facture' AND dv.statut IN ('en_attente','partielle') AND dv.reste > 0
        ORDER BY dv.date_echeance ASC
    """)
    nb_planif = len([r for r in relances if r['statut']=='planifiee'])
    nb_envoy  = len([r for r in relances if r['statut']=='envoyee'])
    total_du  = sum(r['montant_du'] or 0 for r in relances if r['statut'] in ('planifiee','envoyee'))
    return render_template('relances.html', cfg=cfg, relances=relances,
                           clients_dus=clients_dus, factures_dues=factures_dues,
                           nb_planif=nb_planif, nb_envoy=nb_envoy,
                           total_du=total_du, statut_f=statut_f)

@app.route('/relances/add', methods=['POST'])
@login_required
def relance_add():
    f = request.form
    execute("""INSERT INTO relances(client_id,facture_id,date_relance,date_echeance,
               montant_du,type_relance,niveau,message,statut,operateur)
               VALUES(?,?,?,?,?,?,?,?,'planifiee',?)""",
            (f['client_id'], f.get('facture_id') or None,
             f.get('date_relance', date.today().isoformat()),
             f.get('date_echeance'),
             float(f.get('montant_du',0) or 0),
             f.get('type_relance','appel'),
             int(f.get('niveau',1) or 1),
             f.get('message'),
             session.get('user_nom','')))
    flash("Relance créée.", "success")
    return redirect(url_for('relances_list'))

@app.route('/relances/statut/<int:id>', methods=['POST'])
@login_required
def relance_statut(id):
    nouveau = request.form.get('statut','envoyee')
    reponse = request.form.get('reponse','')
    execute("""UPDATE relances SET statut=?, reponse=?,
               date_reponse=CASE WHEN ?='repondue' THEN date('now') ELSE date_reponse END
               WHERE id=?""",
            (nouveau, reponse, nouveau, id))
    flash("Relance mise à jour.", "success")
    return redirect(request.referrer or url_for('relances_list'))

@app.route('/relances/delete/<int:id>')
@login_required
def relance_delete(id):
    execute("DELETE FROM relances WHERE id=?", (id,))
    flash("Relance supprimée.", "success")
    return redirect(url_for('relances_list'))

# ══════════════════════════════════════════════════════════════════════
#  FICHE TIERS (Client / Fournisseur)
# ══════════════════════════════════════════════════════════════════════
@app.route('/clients/<int:id>/fiche')
@login_required
def fiche_client(id):
    cfg = get_cfg()
    client = query("SELECT * FROM clients WHERE id=?", (id,), one=True)
    if not client:
        flash("Client introuvable.", "danger")
        return redirect(url_for('clients_list'))

    # Représentant
    rep = None
    try:
        rep_id = client['representant_id']
        if rep_id:
            rep = query("SELECT * FROM representants WHERE id=?", (rep_id,), one=True)
    except (IndexError, KeyError):
        pass

    # ── Dossier complet (sans limite) ──
    # Déterminer si c'est le client passager (CLI000)
    # → inclure aussi les docs POS enregistrés avec client_id NULL (historique)
    is_passager = (client['code'] == 'CLI000')

    # Devis
    if is_passager:
        devis = query("""
            SELECT * FROM documents_vente
            WHERE (client_id=? OR client_id IS NULL) AND type_doc='devis'
            ORDER BY date_doc DESC
        """, (id,))
    else:
        devis = query("""
            SELECT * FROM documents_vente
            WHERE client_id=? AND type_doc='devis'
            ORDER BY date_doc DESC
        """, (id,))

    # Commandes
    if is_passager:
        commandes = query("""
            SELECT * FROM documents_vente
            WHERE (client_id=? OR client_id IS NULL) AND type_doc='commande'
            ORDER BY date_doc DESC
        """, (id,))
    else:
        commandes = query("""
            SELECT * FROM documents_vente
            WHERE client_id=? AND type_doc='commande'
            ORDER BY date_doc DESC
        """, (id,))

    # Factures — pour le passager inclure aussi client_id NULL (ventes POS anciennes)
    if is_passager:
        factures = query("""
            SELECT dv.*,
                   COALESCE(SUM(r.montant),0) as paye_rgl
            FROM documents_vente dv
            LEFT JOIN reglements r ON r.source_type IN ('facture','vente') AND r.source_id=dv.id
            WHERE dv.type_doc='facture'
              AND (dv.client_id=? OR dv.client_id IS NULL)
            GROUP BY dv.id ORDER BY dv.date_doc DESC
        """, (id,))
    else:
        factures = query("""
            SELECT dv.*,
                   COALESCE(SUM(r.montant),0) as paye_rgl
            FROM documents_vente dv
            LEFT JOIN reglements r ON r.source_type='facture' AND r.source_id=dv.id
            WHERE dv.client_id=? AND dv.type_doc='facture'
            GROUP BY dv.id ORDER BY dv.date_doc DESC
        """, (id,))

    # Règlements — pour le passager inclure aussi source_type='vente' et client_id NULL
    if is_passager:
        reglements = query("""
            SELECT r.*,
                   dv.reference as facture_ref,
                   dv.total_ttc as facture_ttc
            FROM reglements r
            LEFT JOIN documents_vente dv ON dv.id=r.source_id
            WHERE r.client_id=? OR (r.client_id IS NULL AND r.source_type='vente')
            ORDER BY r.date_reglement DESC
        """, (id,))
    else:
        reglements = query("""
            SELECT r.*,
                   dv.reference as facture_ref,
                   dv.total_ttc as facture_ttc
            FROM reglements r
            LEFT JOIN documents_vente dv ON dv.id=r.source_id AND r.source_type='facture'
            WHERE r.client_id=?
            ORDER BY r.date_reglement DESC
        """, (id,))

    # Relances
    relances = query("""
        SELECT rl.*, dv.reference as facture_ref
        FROM relances rl
        LEFT JOIN documents_vente dv ON dv.id=rl.facture_id
        WHERE rl.client_id=?
        ORDER BY rl.date_relance DESC
    """, (id,))

    # Compte courant (mouvements)
    mouvements = query("""
        SELECT * FROM mouvements_compte
        WHERE tiers_type='client' AND tiers_id=?
        ORDER BY date_mvt DESC
    """, (id,))

    # Commissions
    commissions = query("""
        SELECT cm.*, r.nom as rep_nom FROM commissions cm
        JOIN representants r ON r.id=cm.representant_id
        WHERE cm.client_id=? ORDER BY cm.date_commission DESC
    """, (id,))

    # Statistiques globales depuis enregistrement
    if is_passager:
        stats = query("""
            SELECT
                COALESCE(SUM(CASE WHEN type_doc='facture' THEN total_ttc ELSE 0 END),0) as ca_total,
                COALESCE(SUM(CASE WHEN type_doc='facture' THEN reste ELSE 0 END),0) as encours,
                COUNT(CASE WHEN type_doc='facture' THEN 1 END) as nb_factures,
                COUNT(CASE WHEN type_doc='devis' THEN 1 END) as nb_devis,
                COUNT(CASE WHEN type_doc='commande' THEN 1 END) as nb_commandes,
                COALESCE(SUM(CASE WHEN type_doc='facture' THEN montant_paye ELSE 0 END),0) as total_encaisse
            FROM documents_vente
            WHERE client_id=? OR client_id IS NULL
        """, (id,), one=True)
        stats_rgl = query("""
            SELECT COUNT(*) as nb_reglements,
                   COALESCE(SUM(montant),0) as total_rgl
            FROM reglements
            WHERE client_id=? OR (client_id IS NULL AND source_type='vente')
        """, (id,), one=True)
    else:
        stats = query("""
            SELECT
                COALESCE(SUM(CASE WHEN type_doc='facture' THEN total_ttc ELSE 0 END),0) as ca_total,
                COALESCE(SUM(CASE WHEN type_doc='facture' THEN reste ELSE 0 END),0) as encours,
                COUNT(CASE WHEN type_doc='facture' THEN 1 END) as nb_factures,
                COUNT(CASE WHEN type_doc='devis' THEN 1 END) as nb_devis,
                COUNT(CASE WHEN type_doc='commande' THEN 1 END) as nb_commandes,
                COALESCE(SUM(CASE WHEN type_doc='facture' THEN montant_paye ELSE 0 END),0) as total_encaisse
            FROM documents_vente WHERE client_id=?
        """, (id,), one=True)
        stats_rgl = query("""
            SELECT COUNT(*) as nb_reglements,
                   COALESCE(SUM(montant),0) as total_rgl
            FROM reglements WHERE client_id=?
        """, (id,), one=True)

    # Alertes encours
    try:
        plafond = float(client['plafond_credit'] or client['encours_autorise'] or 0)
        encours_actuel = float(stats['encours'] or 0)
        pct_encours = round(encours_actuel / plafond * 100, 1) if plafond > 0 else 0
        alerte_encours = pct_encours >= 80
    except:
        plafond = 0; encours_actuel = 0; pct_encours = 0; alerte_encours = False

    representants = query("SELECT * FROM representants WHERE actif=1 ORDER BY nom")
    factures_dues = [f for f in factures if f['statut'] in ('en_attente','partielle') and (f['reste'] or 0) > 0]

    return render_template('fiche_client.html',
                           cfg=cfg, client=client, rep=rep,
                           devis=devis, commandes=commandes,
                           factures=factures, reglements=reglements,
                           relances=relances, mouvements=mouvements,
                           commissions=commissions,
                           stats=stats, stats_rgl=stats_rgl,
                           representants=representants,
                           factures_dues=factures_dues,
                           plafond=plafond, encours_actuel=encours_actuel,
                           pct_encours=pct_encours, alerte_encours=alerte_encours)


@app.route('/clients/<int:id>/print')
@login_required
def client_print(id):
    from datetime import date as _date
    cfg    = get_cfg()
    client = query("SELECT * FROM clients WHERE id=?", (id,), one=True)
    if not client:
        flash("Client introuvable.", "danger")
        return redirect(url_for('clients_list'))

    factures = query("""
        SELECT dv.*, COALESCE(SUM(r.montant),0) as montant_paye
        FROM documents_vente dv
        LEFT JOIN reglements r ON r.source_type='facture' AND r.source_id=dv.id
        WHERE dv.client_id=? AND dv.type_doc='facture'
        GROUP BY dv.id ORDER BY dv.date_doc DESC
    """, (id,))

    reglements = query("""
        SELECT r.*, dv.reference as facture_ref
        FROM reglements r
        LEFT JOIN documents_vente dv ON dv.id=r.source_id AND r.source_type='facture'
        WHERE r.client_id=?
        ORDER BY r.date_reglement DESC
    """, (id,))

    stats = query("""
        SELECT
            COALESCE(SUM(CASE WHEN type_doc='facture' THEN total_ttc ELSE 0 END),0) as ca_total,
            COALESCE(SUM(CASE WHEN type_doc='facture' THEN reste ELSE 0 END),0)     as encours,
            COUNT(CASE WHEN type_doc='facture' THEN 1 END)                          as nb_factures,
            COALESCE(SUM(CASE WHEN type_doc='facture' THEN montant_paye ELSE 0 END),0) as total_encaisse
        FROM documents_vente WHERE client_id=?
    """, (id,), one=True)

    nom_soc = cfg.get('nom_societe','DISTRIGEST') if cfg else 'DISTRIGEST'
    tel_soc = cfg.get('telephone','')  if cfg else ''
    adr_soc = cfg.get('adresse','')    if cfg else ''
    devise  = cfg.get('devise','FCFA') if cfg else 'FCFA'

    def fmt(v):
        try: return f"{int(float(v)):,}".replace(',', ' ')
        except: return '0'

    # ── Tableau factures ─────────────────────────────────────────────
    modes = {'especes':'💵 Espèces','wave':'📱 Wave','orange_money':'🟠 Orange Money',
             'mtn_money':'🟡 MTN Money','virement':'🏦 Virement','cheque':'📄 Chèque','credit':'🔄 Crédit'}
    statuts_map = {'payee':'✅ Payée','partielle':'⚠ Partielle','en_attente':'❌ Attente','annulee':'Annulée'}

    lignes_html = ''
    tot_ttc = tot_paye = tot_reste = 0
    for f in factures:
        ttc   = float(f['total_ttc']   or 0)
        paye  = float(f['montant_paye'] or 0)
        reste = float(f['reste']        or 0)
        tot_ttc += ttc; tot_paye += paye; tot_reste += reste
        st = f['statut'] or ''
        if st == 'payee':
            sbg, sclr = '#dcfce7','#15803d'
        elif st == 'partielle':
            sbg, sclr = '#fef9c3','#854d0e'
        else:
            sbg, sclr = '#fee2e2','#dc2626'
        badge = f'<span style="background:{sbg};color:{sclr};padding:2px 7px;border-radius:20px;font-size:10px;font-weight:700;">{statuts_map.get(st, st)}</span>'
        lignes_html += (
            f'<tr>'
            f'<td><strong>{f["reference"]}</strong></td>'
            f'<td style="color:#64748b">{f["date_doc"] or "—"}</td>'
            f'<td style="text-align:right;font-weight:700">{fmt(ttc)} {devise}</td>'
            f'<td style="text-align:right;color:#15803d;font-weight:700">{fmt(paye)} {devise}</td>'
            f'<td style="text-align:right;color:{"#dc2626" if reste>0 else "#15803d"};font-weight:700">{fmt(reste)} {devise}</td>'
            f'<td style="text-align:center">{badge}</td>'
            f'</tr>'
        )
    # Ligne total factures
    lignes_html += (
        f'<tr style="background:#1a3a6c;color:white;font-weight:800">'
        f'<td colspan="2">TOTAL FACTURES ({len(list(factures))})</td>'
        f'<td style="text-align:right">{fmt(tot_ttc)} {devise}</td>'
        f'<td style="text-align:right;color:#86efac">{fmt(tot_paye)} {devise}</td>'
        f'<td style="text-align:right;color:{"#fca5a5" if tot_reste>0 else "#86efac"}">{fmt(tot_reste)} {devise}</td>'
        f'<td></td></tr>'
    )

    # ── Séparateur règlements ────────────────────────────────────────
    lignes_html += (
        '<tr><td colspan="6" style="background:#eff6ff;color:#1d4ed8;font-weight:800;'
        'font-size:10px;text-transform:uppercase;letter-spacing:.5px;padding:8px 9px;'
        'border-left:4px solid #1d4ed8;">💳 Règlements reçus</td></tr>'
        '<tr style="background:#1a3a6c;color:white">'
        '<th style="text-align:left">Date</th>'
        '<th style="text-align:left">Référence</th>'
        '<th style="text-align:left">Facture liée</th>'
        '<th style="text-align:left">Mode paiement</th>'
        '<th style="text-align:right">Montant</th>'
        '<th></th></tr>'
    )
    tot_rgl = 0
    for r in reglements:
        m = float(r['montant'] or 0)
        tot_rgl += m
        lignes_html += (
            f'<tr>'
            f'<td style="color:#64748b">{r["date_reglement"] or "—"}</td>'
            f'<td><span style="background:#dbeafe;color:#1d4ed8;padding:2px 6px;border-radius:20px;font-size:10px;font-weight:700">{r["reference"]}</span></td>'
            f'<td style="color:#64748b;font-size:11px">{r["facture_ref"] or "—"}</td>'
            f'<td style="font-size:11px">{modes.get(r["mode_paiement"], r["mode_paiement"] or "—")}</td>'
            f'<td style="text-align:right;color:#15803d;font-weight:800">{fmt(m)} {devise}</td>'
            f'<td></td></tr>'
        )
    lignes_html += (
        f'<tr style="background:#1a3a6c;color:white;font-weight:800">'
        f'<td colspan="4">TOTAL ENCAISSÉ</td>'
        f'<td style="text-align:right;color:#86efac">{fmt(tot_rgl)} {devise}</td>'
        f'<td></td></tr>'
    )

    # ── Appel _print_page_html ───────────────────────────────────────
    ca    = float(stats['ca_total']       or 0)
    encaisse = float(stats['total_encaisse'] or 0)
    encours  = float(stats['encours']        or 0)

    return _journal_print_response(_print_page_html(
        titre='RELEVÉ CLIENT',
        reference=client['code'],
        date_doc=str(_date.today()),
        statut='À jour' if encours == 0 else f'Encours {fmt(encours)} {devise}',
        statut_ok=(encours == 0),
        tiers_label='Client',
        tiers_nom=f"{client['nom']}{' '+client['prenom'] if client['prenom'] else ''}",
        tiers_tel=client['telephone'] or '',
        tiers_adresse=f"{client['adresse'] or ''} {client['ville'] or ''}".strip(),
        nom_soc=nom_soc, tel_soc=tel_soc, adr_soc=adr_soc,
        lignes_html=lignes_html,
        col_headers=['Référence / Date', 'Date', 'Total TTC', 'Payé', 'Reste dû', 'Statut'],
        ht_total=f'CA facturé : {fmt(ca)}',
        tva_total=f'Encaissé : {fmt(encaisse)}',
        ttc_total=fmt(encours),
        reste=fmt(encours) if encours > 0 else '',
        devise=devise,
        doc_statut='payee' if encours == 0 else 'partielle'
    ), 'Fiche_client')

@app.route('/fournisseurs/<int:id>/fiche')
@login_required
def fiche_fournisseur(id):
    cfg = get_cfg()
    fourn = query("SELECT * FROM fournisseurs WHERE id=?", (id,), one=True)
    if not fourn:
        flash("Fournisseur introuvable.", "danger")
        return redirect(url_for('fournisseurs_list'))

    # ── Dossier complet depuis l'enregistrement ──
    commandes = query("""
        SELECT da.*,
               COALESCE(SUM(r.montant),0) as paye_rgl
        FROM documents_achat da
        LEFT JOIN reglements r ON r.fournisseur_id=da.fournisseur_id
            AND r.source_type='achat' AND r.source_id=da.id
        WHERE da.fournisseur_id=?
        GROUP BY da.id ORDER BY da.date_doc DESC
    """, (id,))

    reglements = query("""
        SELECT r.*,
               da.reference as commande_ref
        FROM reglements r
        LEFT JOIN documents_achat da ON da.id=r.source_id AND r.source_type='achat'
        WHERE r.fournisseur_id=?
        ORDER BY r.date_reglement DESC
    """, (id,))

    mouvements = query("""
        SELECT * FROM mouvements_compte
        WHERE tiers_type='fournisseur' AND tiers_id=?
        ORDER BY date_mvt DESC
    """, (id,))

    # Statistiques globales depuis enregistrement
    stats = query("""
        SELECT COALESCE(SUM(total_ttc),0) as total_achats,
               COALESCE(SUM(reste),0) as dettes,
               COALESCE(SUM(montant_paye),0) as total_paye,
               COUNT(*) as nb_commandes,
               COUNT(CASE WHEN statut='recu' THEN 1 END) as nb_recues,
               COUNT(CASE WHEN statut='en_attente' THEN 1 END) as nb_attente
        FROM documents_achat WHERE fournisseur_id=?
    """, (id,), one=True)

    stats_rgl = query("""
        SELECT COUNT(*) as nb_reglements,
               COALESCE(SUM(montant),0) as total_rgl
        FROM reglements WHERE fournisseur_id=?
    """, (id,), one=True)

    # Dernière commande
    derniere_cmd = query("""
        SELECT date_doc FROM documents_achat WHERE fournisseur_id=?
        ORDER BY date_doc DESC LIMIT 1
    """, (id,), one=True)

    # Alertes dettes fournisseur
    try:
        plafond = float(fourn['plafond_credit'] or 0)
        dettes = float(stats['dettes'] or 0)
        pct_dette = round(dettes / plafond * 100, 1) if plafond > 0 else 0
        alerte_dette = dettes > 0
    except:
        plafond = 0; dettes = 0; pct_dette = 0; alerte_dette = False

    return render_template('fiche_fournisseur.html',
                           cfg=cfg, fourn=fourn,
                           commandes=commandes, reglements=reglements,
                           mouvements=mouvements, stats=stats,
                           stats_rgl=stats_rgl,
                           derniere_cmd=derniere_cmd,
                           plafond=plafond, dettes=dettes,
                           pct_dette=pct_dette, alerte_dette=alerte_dette)

# Mise à jour représentant sur un client
@app.route('/clients/<int:id>/set_representant', methods=['POST'])
@login_required
def client_set_representant(id):
    rep_id = request.form.get('representant_id') or None
    execute("UPDATE clients SET representant_id=? WHERE id=?", (rep_id, id))
    flash("Représentant mis à jour.", "success")
    return redirect(url_for('fiche_client', id=id))



# ══════════════════════════════════════════════════════════════════════
#  EMBALLAGES VIDES — GESTION CONSIGNE & CYCLE DE VIE
# ══════════════════════════════════════════════════════════════════════
@app.route('/emballages')
@login_required
def emballages_list():
    cfg = get_cfg()
    if cfg.get('module_emballages') != 'oui':
        flash("Le module Emballages n'est pas activé. Activez-le dans Paramètres.", "warning")
        return redirect(url_for('dashboard'))
    depots = query("SELECT * FROM depots WHERE actif=1 ORDER BY nom")
    depot_f = request.args.get('depot_id','')

    # Types avec stocks agrégés par statut
    types = query("""
        SELECT t.*,
            COALESCE(SUM(CASE WHEN es.statut='disponible'  THEN es.quantite ELSE 0 END),0) as qte_dispo,
            COALESCE(SUM(CASE WHEN es.statut='en_circulation' THEN es.quantite ELSE 0 END),0) as qte_circ,
            COALESCE(SUM(CASE WHEN es.statut='en_reparation' THEN es.quantite ELSE 0 END),0) as qte_rep,
            COALESCE(SUM(CASE WHEN es.statut='reforme'    THEN es.quantite ELSE 0 END),0) as qte_ref,
            COALESCE(SUM(es.quantite),0) as qte_total
        FROM types_emballages t
        LEFT JOIN emballages_stock es ON es.type_id=t.id
        WHERE t.actif=1
        GROUP BY t.id ORDER BY t.categorie, t.nom
    """)

    # Consignes en cours
    consignes = query("""
        SELECT c.*, cl.nom as client_nom, cl.telephone as client_tel,
               t.nom as type_nom, t.couleur
        FROM consignes c
        JOIN clients cl ON cl.id=c.client_id
        JOIN types_emballages t ON t.id=c.type_emballage_id
        WHERE c.statut='en_cours'
        ORDER BY c.date_retour_prevu ASC
    """)

    # Alertes retards
    today = date.today().isoformat()
    alertes = [c for c in consignes if c['date_retour_prevu'] and c['date_retour_prevu'] < today]

    # Réparations en cours
    reparations = query("""
        SELECT r.*, t.nom as type_nom, t.couleur
        FROM reparations_emballages r
        JOIN types_emballages t ON t.id=r.type_id
        WHERE r.statut='en_cours'
        ORDER BY r.date_sortie_prevue ASC
    """)

    # Mouvements récents
    mouvements = query("""
        SELECT m.*, t.nom as type_nom, t.couleur,
               cl.nom as client_nom
        FROM mouvements_emballages m
        JOIN types_emballages t ON t.id=m.type_id
        LEFT JOIN clients cl ON cl.id=m.client_id
        ORDER BY m.date_creation DESC LIMIT 30
    """)

    # KPIs globaux
    stats = query("""
        SELECT
            COALESCE(SUM(CASE WHEN statut='disponible'    THEN quantite ELSE 0 END),0) as total_dispo,
            COALESCE(SUM(CASE WHEN statut='en_circulation' THEN quantite ELSE 0 END),0) as total_circ,
            COALESCE(SUM(CASE WHEN statut='en_reparation' THEN quantite ELSE 0 END),0) as total_rep,
            COALESCE(SUM(CASE WHEN statut='reforme'       THEN quantite ELSE 0 END),0) as total_ref,
            COALESCE(SUM(quantite),0) as total_global
        FROM emballages_stock
    """, one=True)

    valeur_consignes = query("""
        SELECT COALESCE(SUM(montant_total - montant_retourne),0) as val
        FROM consignes WHERE statut='en_cours'
    """, one=True)

    clients = query("SELECT id, nom, telephone FROM clients WHERE actif=1 ORDER BY nom")

    return render_template('emballages.html', cfg=cfg,
                           types=types, consignes=consignes, alertes=alertes,
                           reparations=reparations, mouvements=mouvements,
                           stats=stats, depots=depots, depot_f=depot_f,
                           valeur_consignes=valeur_consignes['val'],
                           nb_alertes=len(alertes), clients=clients)


@app.route('/emballages/mouvement', methods=['POST'])
@login_required
def emballage_mouvement():
    f = request.form
    type_id   = int(f['type_id'])
    depot_id  = int(resolve_depot_id(f.get('depot_id')))
    type_mvt  = f['type_mvt']
    quantite  = int(f.get('quantite', 1) or 1)
    client_id = f.get('client_id') or None

    # Mapping type_mvt → (statut_avant, statut_apres)
    mapping = {
        'mise_en_service':  ('disponible',      'disponible'),
        'sortie_client':    ('disponible',       'en_circulation'),
        'retour_client':    ('en_circulation',   'disponible'),
        'mise_reparation':  ('disponible',       'en_reparation'),
        'fin_reparation':   ('en_reparation',    'disponible'),
        'reforme':          ('disponible',       'reforme'),
        'recyclage':        ('reforme',          None),
        'perte':            ('en_circulation',   None),
    }
    avant, apres = mapping.get(type_mvt, ('disponible','disponible'))

    # Décrémenter stock avant
    if avant:
        execute("""INSERT OR IGNORE INTO emballages_stock(type_id,depot_id,statut,quantite)
                   VALUES(?,?,?,0)""", (type_id, depot_id, avant))
        execute("""UPDATE emballages_stock SET quantite=MAX(0,quantite-?)
                   WHERE type_id=? AND depot_id=? AND statut=?""",
                (quantite, type_id, depot_id, avant))

    # Incrémenter stock après
    if apres:
        execute("""INSERT OR IGNORE INTO emballages_stock(type_id,depot_id,statut,quantite)
                   VALUES(?,?,?,0)""", (type_id, depot_id, apres))
        execute("""UPDATE emballages_stock SET quantite=quantite+?
                   WHERE type_id=? AND depot_id=? AND statut=?""",
                (quantite, type_id, depot_id, apres))

    # Enregistrer mouvement
    execute("""INSERT INTO mouvements_emballages
               (type_id,depot_id,client_id,type_mvt,quantite,statut_avant,statut_apres,notes,operateur)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (type_id, depot_id, client_id, type_mvt, quantite,
             avant, apres, f.get('notes',''), session.get('user_nom','')))

    # Si sortie client → créer consigne
    if type_mvt == 'sortie_client' and client_id:
        te = query("SELECT prix_consigne FROM types_emballages WHERE id=?", (type_id,), one=True)
        pu = te['prix_consigne'] if te else 0
        n  = query("SELECT COUNT(*) as c FROM consignes", one=True)['c']
        ref = f"CON{n+1:05d}"
        execute("""INSERT INTO consignes(reference,client_id,type_emballage_id,quantite_sortie,
                   prix_consigne_unit,montant_total,date_retour_prevu,notes)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (ref, client_id, type_id, quantite, pu, quantite*pu,
                 f.get('date_retour_prevu'), f.get('notes','')))

    # Si retour client → mettre à jour consignes en cours
    if type_mvt == 'retour_client' and client_id:
        consigne_id = f.get('consigne_id')
        if consigne_id:
            cons = query("SELECT * FROM consignes WHERE id=?", (consigne_id,), one=True)
            if cons:
                new_ret = (cons['quantite_retournee'] or 0) + quantite
                new_ret = min(new_ret, cons['quantite_sortie'])
                montant_ret = new_ret * (cons['prix_consigne_unit'] or 0)
                new_statut = 'soldee' if new_ret >= cons['quantite_sortie'] else 'en_cours'
                execute("""UPDATE consignes SET quantite_retournee=?,montant_retourne=?,statut=?
                           WHERE id=?""", (new_ret, montant_ret, new_statut, cons['id']))

    flash(f"Mouvement «{type_mvt.replace('_',' ')}» enregistré ({quantite} unité(s)).", "success")
    return redirect(url_for('emballages_list'))


@app.route('/emballages/reparation/add', methods=['POST'])
@login_required
def reparation_add():
    f = request.form
    execute("""INSERT INTO reparations_emballages
               (type_id,quantite,date_entree,date_sortie_prevue,motif,cout_reparation,notes,operateur)
               VALUES(?,?,?,?,?,?,?,?)""",
            (f['type_id'], int(f.get('quantite',1) or 1),
             f.get('date_entree', date.today().isoformat()),
             f.get('date_sortie_prevue'),
             f.get('motif',''), float(f.get('cout_reparation',0) or 0),
             f.get('notes',''), session.get('user_nom','')))
    # Mettre à jour stock → en_reparation
    type_id  = int(f['type_id'])
    depot_id = resolve_depot_id(f.get('depot_id'))
    qte      = int(f.get('quantite',1) or 1)
    execute("INSERT OR IGNORE INTO emballages_stock(type_id,depot_id,statut,quantite) VALUES(?,?,'disponible',0)",
            (type_id, depot_id))
    execute("UPDATE emballages_stock SET quantite=MAX(0,quantite-?) WHERE type_id=? AND depot_id=? AND statut='disponible'",
            (qte, type_id, depot_id))
    execute("INSERT OR IGNORE INTO emballages_stock(type_id,depot_id,statut,quantite) VALUES(?,?,'en_reparation',0)",
            (type_id, depot_id))
    execute("UPDATE emballages_stock SET quantite=quantite+? WHERE type_id=? AND depot_id=? AND statut='en_reparation'",
            (qte, type_id, depot_id))
    flash("Emballages mis en réparation.", "success")
    return redirect(url_for('emballages_list'))


@app.route('/emballages/reparation/clore/<int:id>', methods=['POST'])
@login_required
def reparation_clore(id):
    f = request.form
    resultat = f.get('resultat','repare')
    cout     = float(f.get('cout_reparation',0) or 0)
    execute("""UPDATE reparations_emballages
               SET statut='terminee', resultat=?, date_sortie_reelle=date('now'), cout_reparation=?, notes=?
               WHERE id=?""", (resultat, cout, f.get('notes',''), id))
    rep = query("SELECT * FROM reparations_emballages WHERE id=?", (id,), one=True)
    if rep:
        type_id = rep['type_id']; qte = rep['quantite']
        depot_id = 1
        execute("INSERT OR IGNORE INTO emballages_stock(type_id,depot_id,statut,quantite) VALUES(?,?,'en_reparation',0)",
                (type_id, depot_id))
        execute("UPDATE emballages_stock SET quantite=MAX(0,quantite-?) WHERE type_id=? AND depot_id=? AND statut='en_reparation'",
                (qte, type_id, depot_id))
        statut_dest = 'disponible' if resultat=='repare' else 'reforme'
        execute("INSERT OR IGNORE INTO emballages_stock(type_id,depot_id,statut,quantite) VALUES(?,?,?,0)",
                (type_id, depot_id, statut_dest))
        execute("UPDATE emballages_stock SET quantite=quantite+? WHERE type_id=? AND depot_id=? AND statut=?",
                (qte, type_id, depot_id, statut_dest))
    flash("Réparation clôturée.", "success")
    return redirect(url_for('emballages_list'))


@app.route('/emballages/type/add', methods=['POST'])
@login_required
def type_emballage_add():
    f = request.form
    n = query("SELECT COUNT(*) as c FROM types_emballages", one=True)['c']
    code = f"EMB{n+10:03d}"
    execute("""INSERT INTO types_emballages
               (code,nom,categorie,contenance,prix_consigne,prix_achat,duree_vie_mois,nb_rotations_max,couleur,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (code, f['nom'], f.get('categorie','bouteille'), f.get('contenance'),
             float(f.get('prix_consigne',0) or 0), float(f.get('prix_achat',0) or 0),
             int(f.get('duree_vie_mois',60) or 60), int(f.get('nb_rotations_max',0) or 0),
             f.get('couleur','#3b82f6'), f.get('notes','')))
    flash("Type d'emballage créé.", "success")
    return redirect(url_for('emballages_list'))


@app.route('/emballages/type/delete/<int:id>')
@login_required
def type_emballage_delete(id):
    execute("UPDATE types_emballages SET actif=0 WHERE id=?", (id,))
    flash("Type d'emballage désactivé.", "success")
    return redirect(url_for('emballages_list'))


@app.route('/consignes/retour/<int:id>', methods=['POST'])
@login_required
def consigne_retour(id):
    f   = request.form
    qte = int(f.get('quantite_retour', 0) or 0)
    cons = query("SELECT * FROM consignes WHERE id=?", (id,), one=True)
    if not cons or qte <= 0:
        flash("Données invalides.", "danger")
        return redirect(url_for('emballages_list'))
    qte_max = cons['quantite_sortie'] - (cons['quantite_retournee'] or 0)
    qte = min(qte, qte_max)
    new_ret = (cons['quantite_retournee'] or 0) + qte
    montant_ret = new_ret * (cons['prix_consigne_unit'] or 0)
    new_statut = 'soldee' if new_ret >= cons['quantite_sortie'] else 'en_cours'
    execute("""UPDATE consignes SET quantite_retournee=?,montant_retourne=?,statut=?
               WHERE id=?""", (new_ret, montant_ret, new_statut, id))
    # Mise à jour stock
    type_id = cons['type_emballage_id']; depot_id = 1
    execute("INSERT OR IGNORE INTO emballages_stock(type_id,depot_id,statut,quantite) VALUES(?,?,'en_circulation',0)",
            (type_id, depot_id))
    execute("UPDATE emballages_stock SET quantite=MAX(0,quantite-?) WHERE type_id=? AND depot_id=? AND statut='en_circulation'",
            (qte, type_id, depot_id))
    execute("INSERT OR IGNORE INTO emballages_stock(type_id,depot_id,statut,quantite) VALUES(?,?,'disponible',0)",
            (type_id, depot_id))
    execute("UPDATE emballages_stock SET quantite=quantite+? WHERE type_id=? AND depot_id=? AND statut='disponible'",
            (qte, type_id, depot_id))
    execute("""INSERT INTO mouvements_emballages(type_id,depot_id,client_id,type_mvt,quantite,statut_avant,statut_apres,operateur)
               VALUES(?,?,?,'retour_client',?,'en_circulation','disponible',?)""",
            (type_id, depot_id, cons['client_id'], qte, session.get('user_nom','')))
    flash(f"{qte} emballage(s) retourné(s). Consigne : {montant_ret:,.0f} FCFA.", "success")
    return redirect(url_for('emballages_list'))


@app.route('/api/emballages/types')
@login_required
def api_emballages_types():
    types = query("SELECT id,code,nom,categorie,prix_consigne,contenance FROM types_emballages WHERE actif=1 ORDER BY nom")
    return jsonify([dict(t) for t in types])



# ══════════════════════════════════════════════════════════════════════
#  COMPTABILITE
# ══════════════════════════════════════════════════════════════════════
MOIS_LABELS = ['Jan','Fev','Mar','Avr','Mai','Jun','Jul','Aou','Sep','Oct','Nov','Dec']

def _build_grand_livre(annee_s, mois_filtre=''):
    """Construit le Grand Livre : liste de mouvements par compte (débit/crédit).
    Chaque recette → Crédit 'Recettes clients' + Débit 'Caisse/Banque'.
    Chaque dépense → Débit compte dépense + Crédit 'Caisse/Banque'."""
    mc = " AND strftime('%m', date_ecriture) = ?" if mois_filtre else ""
    def _a(*a): return list(a) + ([mois_filtre] if mois_filtre else [])

    rows = query("""
        SELECT date_ecriture, type_ecriture, categorie, libelle, montant, source
        FROM ecritures_comptables
        WHERE strftime('%Y', date_ecriture) = ?
    """ + mc + " ORDER BY date_ecriture ASC, id ASC", _a(annee_s))

    mouvements = []
    for r in rows:
        m = float(r['montant'] or 0)
        if r['type_ecriture'] == 'recette':
            mouvements.append({'date': r['date_ecriture'], 'compte': 'Caisse / Banque',
                                'libelle': r['libelle'], 'debit': m, 'credit': 0, 'source': r['source']})
            mouvements.append({'date': r['date_ecriture'], 'compte': r['categorie'] or 'Recettes clients',
                                'libelle': r['libelle'], 'debit': 0, 'credit': m, 'source': r['source']})
        else:
            mouvements.append({'date': r['date_ecriture'], 'compte': r['categorie'] or 'Dépenses diverses',
                                'libelle': r['libelle'], 'debit': m, 'credit': 0, 'source': r['source']})
            mouvements.append({'date': r['date_ecriture'], 'compte': 'Caisse / Banque',
                                'libelle': r['libelle'], 'debit': 0, 'credit': m, 'source': r['source']})

    # Tri par compte puis date
    mouvements.sort(key=lambda x: (x['compte'], x['date']))
    return mouvements


def _build_balance(annee_s):
    """Balance des comptes : totaux débit/crédit par compte depuis le grand livre."""
    gl = _build_grand_livre(annee_s)
    comptes = {}
    for m in gl:
        c = m['compte']
        if c not in comptes:
            comptes[c] = {'compte': c, 'total_debit': 0.0, 'total_credit': 0.0}
        comptes[c]['total_debit']  += m['debit']
        comptes[c]['total_credit'] += m['credit']
    return sorted(comptes.values(), key=lambda x: x['compte'])


def _build_bilan(annee_s, total_recettes, total_depenses, resultat_net, creances, valeur_stock):
    """Bilan simplifié (comptabilité de caisse)."""
    tresorerie = total_recettes - total_depenses

    dettes_fourn_row = query("""
        SELECT COALESCE(SUM(reste), 0) as t
        FROM documents_achat
        WHERE statut NOT IN ('annule','regle')
          AND reste > 0
          AND strftime('%Y', date_doc) <= ?
    """, (annee_s,), one=True)
    dettes_fourn = max(0.0, float(dettes_fourn_row['t'] if dettes_fourn_row else 0) or 0)

    dep_row = query("""
        SELECT COALESCE(SUM(montant), 0) as t
        FROM depenses
        WHERE strftime('%Y', date_depense) = ?
    """, (annee_s,), one=True)
    depenses_non_reglees = float(dep_row['t'] if dep_row else 0) or 0

    total_actif  = max(0.0, tresorerie) + creances + valeur_stock
    total_passif = max(0.0, resultat_net) + dettes_fourn + depenses_non_reglees

    return {
        'tresorerie': max(0.0, tresorerie),
        'creances': creances,
        'valeur_stock': valeur_stock,
        'total_actif': total_actif,
        'resultat_net': resultat_net,
        'total_recettes': total_recettes,
        'total_depenses': total_depenses,
        'dettes_fourn': dettes_fourn,
        'depenses_non_reglees': depenses_non_reglees,
        'total_passif': total_passif,
    }


def _journal_filtre(annee_s, mois_filtre='', type_filtre=''):
    """Retourne les écritures comptables filtrées par année + mois + type.
    Source unique réutilisée par le journal, l'export CSV et l'impression."""
    sql = """SELECT * FROM ecritures_comptables
             WHERE strftime('%Y', date_ecriture) = ?"""
    params = [str(annee_s)]
    if mois_filtre:
        sql += " AND strftime('%m', date_ecriture) = ?"
        params.append(mois_filtre)
    if type_filtre in ('recette', 'depense'):
        sql += " AND type_ecriture = ?"
        params.append(type_filtre)
    sql += " ORDER BY date_ecriture DESC, id DESC"
    return query(sql, params)


@app.route('/comptabilite')
@login_required
def comptabilite():
    """Journal comptable agrégé depuis toutes les sources :
       - règlements clients (recettes)
       - règlements fournisseurs + dépenses d'exploitation (dépenses)
       - écritures manuelles (ajustements)
       Comptabilité de caisse — flux réels uniquement."""
    cfg          = get_cfg()
    annee        = int(request.args.get('annee', datetime.now().year))
    mois_filtre  = request.args.get('mois', '')
    type_filtre  = request.args.get('type', '')
    if type_filtre not in ('recette', 'depense'):
        type_filtre = ''      # '' = tous types
    annee_s      = str(annee)
    mois_courant = f"{datetime.now().month:02d}"
    today_str    = date.today().strftime('%d/%m/%Y')
    tva_taux     = float(cfg.get('tva_collectee_taux', cfg.get('tva', 0)) or 0)

    # Filtre mois optionnel (clause SQL réutilisable)
    mois_clause = " AND strftime('%m', {col}) = ?" if mois_filtre else ""
    def _args(*a):
        return list(a) + ([mois_filtre] if mois_filtre else [])

    # ── 1) LIGNES RECETTES (règlements clients + recettes manuelles) ─
    sql_rec_regl = """
        SELECT
            r.id, r.reference, r.date_reglement, r.montant,
            r.mode_paiement, r.notes, r.client_id,
            COALESCE(c.nom || COALESCE(' ' || c.prenom, ''), '—') as client_nom,
            COALESCE(r.source_type, 'vente') as source_type,
            'reglement' as source,
            'Règlements clients' as categorie
        FROM reglements r
        LEFT JOIN clients c ON c.id = r.client_id
        WHERE strftime('%Y', r.date_reglement) = ?
          AND r.client_id IS NOT NULL
    """ + mois_clause.format(col='r.date_reglement') + """
        ORDER BY r.date_reglement DESC, r.id DESC
    """
    rec_regl_rows = query(sql_rec_regl, _args(annee_s))

    sql_rec_manu = """
        SELECT
            id, libelle as reference,
            date_ecriture as date_reglement, montant,
            mode_paiement, notes,
            NULL as client_id,
            libelle as client_nom,
            'manuel' as source_type,
            'manuel' as source,
            categorie
        FROM ecritures_comptables
        WHERE strftime('%Y', date_ecriture) = ?
          AND type_ecriture = 'recette'
          AND COALESCE(source, '') NOT IN ('reglement')
    """ + mois_clause.format(col='date_ecriture') + """
        ORDER BY date_ecriture DESC, id DESC
    """
    rec_manu_rows = query(sql_rec_manu, _args(annee_s))

    lignes_recettes = [dict(r) for r in rec_regl_rows] + [dict(r) for r in rec_manu_rows]
    lignes_recettes.sort(key=lambda r: (r.get('date_reglement') or '', r.get('id') or 0), reverse=True)

    # ── 2) LIGNES DÉPENSES (règlements fournisseurs + dépenses d'expl. + manuelles) ─
    sql_dep_fourn = """
        SELECT
            r.id, r.reference,
            r.date_reglement as date_depense,
            r.montant, r.mode_paiement, r.notes,
            COALESCE('Règlement fournisseur ' || f.nom, r.notes, 'Règlement fournisseur') as description,
            'Règlements fournisseurs' as categorie,
            'reglement_fourn' as source
        FROM reglements r
        LEFT JOIN fournisseurs f ON f.id = r.fournisseur_id
        WHERE strftime('%Y', r.date_reglement) = ?
          AND r.fournisseur_id IS NOT NULL
    """ + mois_clause.format(col='r.date_reglement') + """
        ORDER BY r.date_reglement DESC, r.id DESC
    """
    dep_fourn_rows = query(sql_dep_fourn, _args(annee_s))

    sql_dep_op = """
        SELECT
            id,
            'DEP-' || printf('%04d', id) as reference,
            date_depense, categorie,
            description, montant,
            NULL as mode_paiement,
            notes, responsable,
            'depense' as source
        FROM depenses
        WHERE strftime('%Y', date_depense) = ?
    """ + mois_clause.format(col='date_depense') + """
        ORDER BY date_depense DESC, id DESC
    """
    dep_op_rows = query(sql_dep_op, _args(annee_s))

    sql_dep_manu = """
        SELECT
            id, libelle as reference,
            date_ecriture as date_depense,
            categorie,
            libelle as description, montant,
            mode_paiement, notes,
            'manuel' as source
        FROM ecritures_comptables
        WHERE strftime('%Y', date_ecriture) = ?
          AND type_ecriture = 'depense'
          AND COALESCE(source, '') NOT IN ('depense', 'reglement', 'achat', 'reglement_achat')
    """ + mois_clause.format(col='date_ecriture') + """
        ORDER BY date_ecriture DESC, id DESC
    """
    dep_manu_rows = query(sql_dep_manu, _args(annee_s))

    lignes_depenses = (
        [dict(r) for r in dep_fourn_rows]
        + [dict(r) for r in dep_op_rows]
        + [dict(r) for r in dep_manu_rows]
    )
    lignes_depenses.sort(key=lambda r: (r.get('date_depense') or '', r.get('id') or 0), reverse=True)

    # ── 3) KPIs annuels — totalisés sur les ÉCRITURES COMPTABLES ──────
    #  Le Journal, les panneaux (par_cat) et le graphique lisent tous
    #  ecritures_comptables. Les totaux du bandeau doivent donc sommer la
    #  MÊME source, sinon on a l'incohérence « journal rempli mais total à 0 »
    #  (ou un total recettes gonflé par un double comptage règlement+facture).
    _tr = query("""SELECT COALESCE(SUM(montant),0) as t FROM ecritures_comptables
                   WHERE strftime('%Y', date_ecriture)=? AND type_ecriture='recette'
                """ + mois_clause.format(col='date_ecriture'), _args(annee_s), one=True)
    _td = query("""SELECT COALESCE(SUM(montant),0) as t FROM ecritures_comptables
                   WHERE strftime('%Y', date_ecriture)=? AND type_ecriture='depense'
                """ + mois_clause.format(col='date_ecriture'), _args(annee_s), one=True)
    total_recettes = float(_tr['t'] or 0) if _tr else 0
    total_depenses = float(_td['t'] or 0) if _td else 0
    resultat_net   = total_recettes - total_depenses

    # CA du mois courant (toujours, indépendamment du filtre)
    ca_mois_row = query("""SELECT COALESCE(SUM(montant),0) as t FROM reglements
                           WHERE strftime('%Y', date_reglement)=?
                             AND strftime('%m', date_reglement)=?
                             AND client_id IS NOT NULL""",
                       (annee_s, mois_courant), one=True)
    ca_mois = float(ca_mois_row['t'] or 0) if ca_mois_row else 0

    # ── Résultat net du mois : MÊME source que les totaux annuels (écritures) ──
    _rm = query("""SELECT COALESCE(SUM(montant),0) as t FROM ecritures_comptables
                   WHERE strftime('%Y', date_ecriture)=? AND strftime('%m', date_ecriture)=?
                     AND type_ecriture='recette'""",
                (annee_s, mois_courant), one=True)
    rec_mois = float(_rm['t'] or 0) if _rm else 0
    _dm = query("""SELECT COALESCE(SUM(montant),0) as t FROM ecritures_comptables
                   WHERE strftime('%Y', date_ecriture)=? AND strftime('%m', date_ecriture)=?
                     AND type_ecriture='depense'""",
                (annee_s, mois_courant), one=True)
    dep_mois = float(_dm['t'] or 0) if _dm else 0

    resultat_mois = rec_mois - dep_mois

    # Créances clients = reste à payer sur factures émises
    creances_row = query("""SELECT COALESCE(SUM(reste),0) as t FROM documents_vente
                            WHERE type_doc='facture'
                              AND statut IN ('en_attente','partielle')
                              AND reste > 0""", one=True)
    creances = float(creances_row['t'] or 0) if creances_row else 0

    # Valeur du stock (au coût d'achat HT)
    stock_row = query("""SELECT COALESCE(SUM(s.quantite_unite * a.prix_achat_ht), 0) as t
                         FROM stocks s
                         LEFT JOIN articles a ON a.id = s.article_id
                         WHERE a.actif = 1""", one=True)
    valeur_stock = float(stock_row['t'] or 0) if stock_row else 0

    # ── 4) Répartition par mois (pour le graphique) ─────────────
    mois_dict = {f"{m:02d}": {'mois': f"{m:02d}", 'recettes': 0.0, 'depenses': 0.0}
                 for m in range(1, 13)}

    # Graphique alimenté par les ÉCRITURES COMPTABLES (même source que les
    # totaux, les panneaux et le journal) → tableau de bord parfaitement cohérent.
    rows = query("""SELECT strftime('%m', date_ecriture) as mois, type_ecriture,
                           COALESCE(SUM(montant), 0) as t
                    FROM ecritures_comptables
                    WHERE strftime('%Y', date_ecriture) = ?
                    GROUP BY mois, type_ecriture""",
                 (annee_s,))
    for r in rows:
        if r['mois']:
            k = 'recettes' if r['type_ecriture'] == 'recette' else 'depenses'
            mois_dict[r['mois']][k] += float(r['t'] or 0)

    par_mois = [mois_dict[k] for k in sorted(mois_dict.keys())]

    # ── 5) Années disponibles (depuis toutes les sources) ───────
    rows = query("""
        SELECT annee FROM (
            SELECT DISTINCT strftime('%Y', date_reglement) as annee FROM reglements        WHERE date_reglement IS NOT NULL
            UNION SELECT DISTINCT strftime('%Y', date_depense)     FROM depenses           WHERE date_depense   IS NOT NULL
            UNION SELECT DISTINCT strftime('%Y', date_ecriture)    FROM ecritures_comptables WHERE date_ecriture IS NOT NULL
        ) WHERE annee IS NOT NULL ORDER BY annee DESC
    """)
    annees_dispo = [r['annee'] for r in rows]
    if not annees_dispo:
        annees_dispo = [str(datetime.now().year)]
    if annee_s not in annees_dispo:
        annees_dispo.insert(0, annee_s)

    # ── 6) Répartition par catégorie ──────────────────────────────────
    par_cat_recettes = [dict(r) for r in query("""
        SELECT COALESCE(categorie,'Recettes') as categorie, COALESCE(SUM(montant),0) as total
        FROM ecritures_comptables
        WHERE strftime('%Y',date_ecriture)=? AND type_ecriture='recette'
        GROUP BY categorie ORDER BY total DESC""", (annee_s,))]

    par_cat_depenses = [dict(r) for r in query("""
        SELECT COALESCE(categorie,'Dépenses') as categorie, COALESCE(SUM(montant),0) as total
        FROM ecritures_comptables
        WHERE strftime('%Y',date_ecriture)=? AND type_ecriture='depense'
        GROUP BY categorie ORDER BY total DESC""", (annee_s,))]

    # ── Journal des écritures : filtré par année + mois + type ───────
    ecritures_filtrees = _journal_filtre(annee_s, mois_filtre, type_filtre)

    return render_template('comptabilite.html',
                           cfg=cfg,
                           annee=annee, mois_filtre=mois_filtre,
                           today=today_str,
                           tva_taux=tva_taux,
                           lignes_recettes=lignes_recettes,
                           lignes_depenses=lignes_depenses,
                           total_recettes=total_recettes,
                           total_depenses=total_depenses,
                           resultat_net=resultat_net,
                           ca_mois=ca_mois,
                           resultat_mois=resultat_mois,
                           rec_mois=rec_mois,
                           dep_mois=dep_mois,
                           creances=creances,
                           valeur_stock=valeur_stock,
                           par_mois=par_mois,
                           annees_dispo=annees_dispo,
                           ecritures=ecritures_filtrees,
                           type_filtre=type_filtre,
                           par_cat_recettes=par_cat_recettes,
                           par_cat_depenses=par_cat_depenses,
                           grand_livre=_build_grand_livre(annee_s, mois_filtre),
                           balance=_build_balance(annee_s),
                           bilan=_build_bilan(annee_s, total_recettes, total_depenses,
                                              resultat_net, creances, valeur_stock),
                           MOIS_LABELS=MOIS_LABELS)


# ════════════════════════════════════════════════════════════════════
#  COMPTABILITÉ — EXPORT / IMPRESSION / IMPORT  (journal filtré)
# ════════════════════════════════════════════════════════════════════
_MODES_LBL = {
    'especes': 'Espèces', 'carte_bancaire': 'Carte', 'wave': 'Wave',
    'orange_money': 'Orange Money', 'mtn_money': 'MTN Money',
    'moov_money': 'Moov Money', 'virement': 'Virement', 'cheque': 'Chèque',
}
_MOIS_FULL = ['Janvier', 'Février', 'Mars', 'Avril', 'Mai', 'Juin', 'Juillet',
              'Août', 'Septembre', 'Octobre', 'Novembre', 'Décembre']


def _periode_label(annee_s, mois_filtre, type_filtre):
    """Construit un libellé lisible de la période + type filtrés."""
    periode = _MOIS_FULL[int(mois_filtre) - 1] + ' ' + str(annee_s) if mois_filtre else 'Année ' + str(annee_s)
    typ = {'recette': 'Recettes uniquement', 'depense': 'Dépenses uniquement'}.get(type_filtre, 'Tous types')
    return periode, typ


@app.route('/comptabilite/export.csv')
@login_required
def comptabilite_export_csv():
    """Exporte le journal filtré (année/mois/type) au format CSV."""
    import csv
    annee_s     = str(request.args.get('annee', datetime.now().year))
    mois_filtre = request.args.get('mois', '')
    type_filtre = request.args.get('type', '')
    if type_filtre not in ('recette', 'depense'):
        type_filtre = ''

    ecritures = _journal_filtre(annee_s, mois_filtre, type_filtre)

    buf = io.StringIO()
    buf.write('\ufeff')  # BOM UTF-8 → accents corrects dans Excel
    w = csv.writer(buf, delimiter=';')
    w.writerow(['Date', 'Type', 'Catégorie', 'Libellé',
                'Mode paiement', 'Source', 'Montant', 'Devise'])
    devise = (get_cfg().get('devise') or 'FCFA')
    total_rec = total_dep = 0.0
    for e in ecritures:
        e = dict(e)
        d = e.get('date_ecriture') or ''
        d_fr = f"{d[8:10]}/{d[5:7]}/{d[0:4]}" if len(d) >= 10 else d
        montant = float(e.get('montant') or 0)
        if e.get('type_ecriture') == 'recette':
            total_rec += montant
        else:
            total_dep += montant
        w.writerow([
            d_fr,
            'Recette' if e.get('type_ecriture') == 'recette' else 'Dépense',
            e.get('categorie') or '',
            e.get('libelle') or '',
            _MODES_LBL.get(e.get('mode_paiement'), e.get('mode_paiement') or ''),
            (e.get('source') or 'Manuel').capitalize(),
            f"{montant:.0f}",
            devise,
        ])
    # Lignes de totaux
    w.writerow([])
    w.writerow(['', '', '', '', '', 'TOTAL RECETTES', f"{total_rec:.0f}", devise])
    w.writerow(['', '', '', '', '', 'TOTAL DÉPENSES', f"{total_dep:.0f}", devise])
    w.writerow(['', '', '', '', '', 'RÉSULTAT NET',   f"{total_rec - total_dep:.0f}", devise])

    from flask import Response
    suffixe = (f"{annee_s}"
               + (f"_{mois_filtre}" if mois_filtre else "")
               + (f"_{type_filtre}" if type_filtre else ""))
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition':
                 f'attachment; filename="journal_comptable_{suffixe}.csv"'})


@app.route('/comptabilite/imprimer')
@login_required
def comptabilite_imprimer():
    """Page imprimable du journal filtré (Imprimer / Enregistrer en PDF)."""
    cfg         = get_cfg()
    annee_s     = str(request.args.get('annee', datetime.now().year))
    mois_filtre = request.args.get('mois', '')
    type_filtre = request.args.get('type', '')
    if type_filtre not in ('recette', 'depense'):
        type_filtre = ''

    ecritures = [dict(e) for e in _journal_filtre(annee_s, mois_filtre, type_filtre)]
    periode, typ_lbl = _periode_label(annee_s, mois_filtre, type_filtre)
    devise = (cfg.get('devise') or 'FCFA')
    soc    = (cfg.get('nom_depot') or cfg.get('entreprise_nom') or 'DISTRIGEST')
    _soc   = _infos_entreprise()

    total_rec = sum(float(e.get('montant') or 0) for e in ecritures if e.get('type_ecriture') == 'recette')
    total_dep = sum(float(e.get('montant') or 0) for e in ecritures if e.get('type_ecriture') == 'depense')

    def _fmt(v):
        return f"{float(v or 0):,.0f}".replace(',', ' ')

    def _d(d):
        d = d or ''
        return f"{d[8:10]}/{d[5:7]}/{d[0:4]}" if len(d) >= 10 else d

    lignes = []
    for e in ecritures:
        rec = e.get('type_ecriture') == 'recette'
        clr = '#15803d' if rec else '#dc2626'
        sign = '+' if rec else '−'
        src = (e.get('source') or 'Manuel').capitalize()
        lignes.append(f"""
        <tr>
          <td>{_d(e.get('date_ecriture'))}</td>
          <td><span style="color:{clr};font-weight:700;">{'Recette' if rec else 'Dépense'}</span></td>
          <td>{e.get('categorie') or ''}</td>
          <td>{e.get('libelle') or ''}</td>
          <td>{_MODES_LBL.get(e.get('mode_paiement'), e.get('mode_paiement') or '—')}</td>
          <td>{src}</td>
          <td style="text-align:right;font-weight:700;color:{clr};">{sign}{_fmt(e.get('montant'))}</td>
        </tr>""")
    if not lignes:
        lignes.append('<tr><td colspan="7" style="text-align:center;padding:24px;color:#64748b;">Aucune écriture pour cette sélection.</td></tr>')

    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<title>Journal comptable — {periode}</title>
<style>
  *{{box-sizing:border-box;}}
  body{{font-family:'Segoe UI',Arial,sans-serif;color:#0f172a;margin:0;padding:28px;}}
  .hd{{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:3px solid #2563eb;padding-bottom:14px;margin-bottom:18px;}}
  .hd h1{{font-size:20px;margin:0 0 4px;}}
  .hd .soc{{font-size:15px;font-weight:700;color:#2563eb;}}
  .hd .meta{{font-size:12px;color:#64748b;text-align:right;line-height:1.6;}}
  .tag{{display:inline-block;background:#eff6ff;color:#2563eb;border:1px solid #bfdbfe;border-radius:20px;padding:2px 10px;font-size:11px;font-weight:700;margin-top:4px;}}
  table{{width:100%;border-collapse:collapse;font-size:12px;}}
  thead th{{background:#1e3a8a;color:#fff;text-align:left;padding:8px 10px;font-size:11px;text-transform:uppercase;letter-spacing:.4px;}}
  tbody td{{padding:7px 10px;border-bottom:1px solid #e2e8f0;}}
  tbody tr:nth-child(even){{background:#f8fafc;}}
  .tot{{margin-top:18px;display:flex;gap:14px;justify-content:flex-end;flex-wrap:wrap;}}
  .tot .box{{border:1px solid #e2e8f0;border-radius:10px;padding:10px 16px;min-width:150px;}}
  .tot .box .lbl{{font-size:10px;text-transform:uppercase;color:#64748b;font-weight:700;letter-spacing:.4px;}}
  .tot .box .val{{font-size:18px;font-weight:800;margin-top:2px;}}
  .ft{{margin-top:26px;font-size:10px;color:#94a3b8;border-top:1px solid #e2e8f0;padding-top:8px;}}
  .pbtn{{position:fixed;top:14px;right:14px;background:#2563eb;color:#fff;border:none;border-radius:8px;padding:10px 18px;font-size:13px;font-weight:700;cursor:pointer;box-shadow:0 4px 12px rgba(37,99,235,.3);}}
  @media print{{ .pbtn{{display:none;}} body{{padding:0;}} @page{{margin:14mm;}} }}
</style></head>
<body>
  <button class="pbtn" onclick="window.print()">🖨️ Imprimer / PDF</button>
  <div class="hd">
    <div>
      <div class="soc">{soc}</div>
      <div style="font-size:10.5px;color:#64748b;line-height:1.55;margin:2px 0 6px;">
        {_soc['adresse']}{(' · Tél : ' + _soc['tel']) if _soc['tel'] else ''}{(' · ' + _soc['email']) if _soc['email'] else ''}<br>
        {('NCC : ' + _soc['ncc']) if _soc['ncc'] else ''}{' · ' if _soc['ncc'] and _soc['rccm'] else ''}{('RCCM : ' + _soc['rccm']) if _soc['rccm'] else ''}
      </div>
      <h1>Journal comptable</h1>
      <span class="tag">{typ_lbl}</span>
    </div>
    <div class="meta">
      <div><strong>Période :</strong> {periode}</div>
      <div><strong>Écritures :</strong> {len(ecritures)}</div>
      <div>Édité le {datetime.now().strftime('%d/%m/%Y à %H:%M')}</div>
    </div>
  </div>
  <table>
    <thead><tr>
      <th>Date</th><th>Type</th><th>Catégorie</th><th>Libellé</th>
      <th>Mode</th><th>Source</th><th style="text-align:right;">Montant ({devise})</th>
    </tr></thead>
    <tbody>{''.join(lignes)}</tbody>
  </table>
  <div class="tot">
    <div class="box"><div class="lbl">Total recettes</div><div class="val" style="color:#15803d;">+{_fmt(total_rec)} {devise}</div></div>
    <div class="box"><div class="lbl">Total dépenses</div><div class="val" style="color:#dc2626;">−{_fmt(total_dep)} {devise}</div></div>
    <div class="box" style="background:#f0f9ff;border-color:#bfdbfe;"><div class="lbl">Résultat net</div><div class="val" style="color:{'#15803d' if total_rec-total_dep>=0 else '#dc2626'};">{'+' if total_rec-total_dep>=0 else '−'}{_fmt(abs(total_rec-total_dep))} {devise}</div></div>
  </div>
  <div class="ft">Document généré par DISTRIGEST — comptabilité de caisse. Ce journal reflète les filtres appliqués au moment de l'édition.</div>
  <script>window.addEventListener('load', function(){{ setTimeout(function(){{ window.print(); }}, 350); }});</script>
</body></html>"""
    return _journal_print_response(html, 'Journal_comptable')


@app.route('/comptabilite/importer-csv', methods=['POST'])
@login_required
def comptabilite_importer_csv():
    """Importe des écritures manuelles depuis un CSV (mêmes colonnes que l'export).
    Colonnes attendues : Date ; Type ; Catégorie ; Libellé ; Mode paiement ; Source ; Montant
    Les lignes importées sont enregistrées comme écritures manuelles (supprimables)."""
    import csv as _csv
    fichier = request.files.get('fichier_csv')
    if not fichier or not fichier.filename.lower().endswith('.csv'):
        flash("Veuillez sélectionner un fichier .csv valide.", "danger")
        return redirect(url_for('comptabilite'))

    try:
        contenu = fichier.read().decode('utf-8-sig', errors='replace')
    except Exception:
        flash("Impossible de lire le fichier CSV.", "danger")
        return redirect(url_for('comptabilite'))

    # Détection du séparateur (; ou ,)
    sep = ';' if contenu.count(';') >= contenu.count(',') else ','
    reader = _csv.reader(io.StringIO(contenu), delimiter=sep)
    rows = list(reader)
    if not rows:
        flash("Fichier CSV vide.", "warning")
        return redirect(url_for('comptabilite'))

    # Sauter l'entête si présent
    start = 1 if rows and rows[0] and rows[0][0].strip().lower() in ('date', 'date ') else 0
    rev_modes = {v.lower(): k for k, v in _MODES_LBL.items()}

    nb_ok = nb_skip = 0
    for r in rows[start:]:
        if not r or len([c for c in r if c.strip()]) < 3:
            continue
        # Ignorer les lignes de totaux de l'export
        if r and r[0].strip() == '' and any('TOTAL' in (c or '').upper() or 'RÉSULTAT' in (c or '').upper() for c in r):
            continue
        try:
            d_raw  = (r[0] or '').strip()
            typ    = (r[1] or '').strip().lower() if len(r) > 1 else ''
            cat    = (r[2] or '').strip() if len(r) > 2 else 'Divers'
            lib    = (r[3] or '').strip() if len(r) > 3 else ''
            mode   = (r[4] or '').strip() if len(r) > 4 else ''
            montant_raw = (r[6] if len(r) > 6 else r[-1]) or '0'
            montant = float(str(montant_raw).replace(' ', '').replace('\xa0', '').replace(',', '.') or 0)

            if montant <= 0 or not lib:
                nb_skip += 1
                continue

            type_ecr = 'recette' if typ.startswith('rec') else 'depense'

            # Date JJ/MM/AAAA → AAAA-MM-JJ ; sinon on garde tel quel si déjà ISO
            if '/' in d_raw and len(d_raw) >= 8:
                p = d_raw.split('/')
                date_iso = f"{p[2]}-{p[1].zfill(2)}-{p[0].zfill(2)}" if len(p) == 3 else d_raw
            else:
                date_iso = d_raw or date.today().isoformat()

            mode_db = rev_modes.get(mode.lower(), mode.lower().replace(' ', '_')) if mode and mode != '—' else None

            execute("""INSERT INTO ecritures_comptables
                       (type_ecriture, date_ecriture, categorie, libelle, montant, mode_paiement)
                       VALUES(?,?,?,?,?,?)""",
                    (type_ecr, date_iso, cat or 'Divers', lib, montant, mode_db))
            nb_ok += 1
        except Exception as _e:
            logging.warning("[IMPORT CSV] ligne ignorée : %s", _e)
            nb_skip += 1

    if nb_ok:
        flash(f"✅ {nb_ok} écriture(s) importée(s)"
              + (f", {nb_skip} ignorée(s)." if nb_skip else "."), "success")
    else:
        flash(f"Aucune écriture importée ({nb_skip} ligne(s) ignorée(s)). "
              "Vérifiez le format du fichier.", "warning")
    return redirect(url_for('comptabilite'))


@app.route('/comptabilite/rapport')
@login_required
def comptabilite_rapport():
    cfg = get_cfg()
    annee = int(request.args.get('annee', date.today().year))
    MOIS_LABELS = ['Jan','Fév','Mar','Avr','Mai','Jun','Jul','Aoû','Sep','Oct','Nov','Déc']
    MOIS_FULL   = ['Janvier','Février','Mars','Avril','Mai','Juin','Juillet','Août','Septembre','Octobre','Novembre','Décembre']

    # ── Totaux annuels ──────────────────────────────────────────────
    totaux = query("""SELECT
        COALESCE(SUM(CASE WHEN type_ecriture='recette' THEN montant ELSE 0 END),0) as total_recettes,
        COALESCE(SUM(CASE WHEN type_ecriture='depense' THEN montant ELSE 0 END),0) as total_depenses,
        COUNT(*) as nb_ecritures,
        COUNT(CASE WHEN type_ecriture='recette' THEN 1 END) as nb_recettes,
        COUNT(CASE WHEN type_ecriture='depense' THEN 1 END) as nb_depenses
        FROM ecritures_comptables WHERE strftime('%Y',date_ecriture)=?""", (str(annee),), one=True)
    total_recettes = totaux['total_recettes'] or 0
    total_depenses = totaux['total_depenses'] or 0
    resultat_net   = total_recettes - total_depenses
    marge_nette    = round((resultat_net / total_recettes * 100), 1) if total_recettes else 0

    # ── Mensuel ─────────────────────────────────────────────────────
    par_mois = query("""SELECT strftime('%m',date_ecriture) as mois,
        COALESCE(SUM(CASE WHEN type_ecriture='recette' THEN montant ELSE 0 END),0) as recettes,
        COALESCE(SUM(CASE WHEN type_ecriture='depense' THEN montant ELSE 0 END),0) as depenses
        FROM ecritures_comptables WHERE strftime('%Y',date_ecriture)=?
        GROUP BY mois ORDER BY mois""", (str(annee),))

    # ── Par catégorie ────────────────────────────────────────────────
    par_cat_rec = query("""SELECT categorie,
        COALESCE(SUM(montant),0) as total, COUNT(*) as nb
        FROM ecritures_comptables WHERE type_ecriture='recette' AND strftime('%Y',date_ecriture)=?
        GROUP BY categorie ORDER BY total DESC""", (str(annee),))
    par_cat_dep = query("""SELECT categorie,
        COALESCE(SUM(montant),0) as total, COUNT(*) as nb
        FROM ecritures_comptables WHERE type_ecriture='depense' AND strftime('%Y',date_ecriture)=?
        GROUP BY categorie ORDER BY total DESC""", (str(annee),))

    # ── Top clients ─────────────────────────────────────────────────
    top_clients = query("""SELECT c.nom, COALESCE(SUM(r.montant),0) as total
        FROM reglements r JOIN clients c ON c.id=r.client_id
        WHERE strftime('%Y',r.date_reglement)=? AND r.client_id IS NOT NULL
        GROUP BY r.client_id ORDER BY total DESC LIMIT 5""", (str(annee),))

    # ── Journal complet ─────────────────────────────────────────────
    ecritures = query("""SELECT * FROM ecritures_comptables
        WHERE strftime('%Y',date_ecriture)=?
        ORDER BY date_ecriture DESC, id DESC""", (str(annee),))

    # ── Solde mensuel cumulé ────────────────────────────────────────
    solde_cumul = 0
    soldes = []
    for m in range(1, 13):
        mois_str = f"{annee}-{m:02d}"
        r = query("""SELECT
            COALESCE(SUM(CASE WHEN type_ecriture='recette' THEN montant ELSE 0 END),0) as rec,
            COALESCE(SUM(CASE WHEN type_ecriture='depense' THEN montant ELSE 0 END),0) as dep
            FROM ecritures_comptables WHERE strftime('%Y-%m',date_ecriture)=?""", (mois_str,), one=True)
        solde_cumul += (r['rec'] or 0) - (r['dep'] or 0)
        soldes.append({'mois': m, 'label': MOIS_LABELS[m-1], 'label_full': MOIS_FULL[m-1],
                       'recettes': r['rec'], 'depenses': r['dep'], 'solde_cumul': solde_cumul})

    def fcfa(v):
        try: return f"{int(float(v or 0)):,}".replace(',', ' ')
        except: return '0'

    nom_soc  = cfg.get('nom_depot', 'DISTRIGEST')
    adr_soc  = cfg.get('adresse', '')
    tel_soc  = cfg.get('telephone', '')
    _soc     = _infos_entreprise()
    devise   = cfg.get('devise', 'FCFA')
    now_str  = date.today().strftime('%d/%m/%Y')

    # ── Construire HTML rapport ──────────────────────────────────────
    # Lignes mensuel
    rows_mois = ''
    for s in soldes:
        if s['recettes'] == 0 and s['depenses'] == 0:
            continue
        net = (s['recettes'] or 0) - (s['depenses'] or 0)
        clr = '#15803d' if net >= 0 else '#dc2626'
        rows_mois += f"""<tr>
          <td style="font-weight:600;">{s['label_full']}</td>
          <td style="text-align:right;color:#15803d;font-weight:700;">{fcfa(s['recettes'])} {devise}</td>
          <td style="text-align:right;color:#dc2626;font-weight:700;">{fcfa(s['depenses'])} {devise}</td>
          <td style="text-align:right;color:{clr};font-weight:800;">{'+' if net>=0 else '−'}{fcfa(abs(net))} {devise}</td>
          <td style="text-align:right;color:{'#15803d' if s['solde_cumul']>=0 else '#dc2626'};font-weight:700;">{fcfa(s['solde_cumul'])} {devise}</td>
        </tr>"""

    # Catégories recettes
    rows_cat_rec = ''
    for pc in par_cat_rec:
        pct = round(pc['total']/total_recettes*100) if total_recettes else 0
        rows_cat_rec += f"""<tr>
          <td>{pc['categorie']}</td>
          <td style="text-align:center;">{pc['nb']}</td>
          <td style="text-align:right;color:#15803d;font-weight:700;">{fcfa(pc['total'])} {devise}</td>
          <td style="text-align:right;color:#64748b;">{pct}%</td>
        </tr>"""

    # Catégories dépenses
    rows_cat_dep = ''
    for pc in par_cat_dep:
        pct = round(pc['total']/total_depenses*100) if total_depenses else 0
        rows_cat_dep += f"""<tr>
          <td>{pc['categorie']}</td>
          <td style="text-align:center;">{pc['nb']}</td>
          <td style="text-align:right;color:#dc2626;font-weight:700;">{fcfa(pc['total'])} {devise}</td>
          <td style="text-align:right;color:#64748b;">{pct}%</td>
        </tr>"""

    # Top clients
    rows_clients = ''
    for cl in top_clients:
        rows_clients += f"""<tr>
          <td style="font-weight:600;">{cl['nom']}</td>
          <td style="text-align:right;color:#15803d;font-weight:700;">{fcfa(cl['total'])} {devise}</td>
        </tr>"""

    # Journal complet
    rows_journal = ''
    modes_map = {'especes':'Espèces','carte_bancaire':'Carte','wave':'Wave','orange_money':'Orange M.','mtn_money':'MTN','moov_money':'Moov','virement':'Virement','cheque':'Chèque'}
    for e in ecritures:
        clr  = '#15803d' if e['type_ecriture']=='recette' else '#dc2626'
        sign = '+' if e['type_ecriture']=='recette' else '−'
        mode = modes_map.get(e['mode_paiement'] or '', e['mode_paiement'] or '—')
        d    = e['date_ecriture']
        date_fmt = f"{d[8:10]}/{d[5:7]}/{d[0:4]}" if d else '—'
        src  = e['source'] or 'Manuel'
        rows_journal += f"""<tr style="border-bottom:1px solid #f1f5f9;">
          <td style="font-size:11px;color:#64748b;">{date_fmt}</td>
          <td><span style="padding:2px 8px;border-radius:12px;font-size:10px;font-weight:700;background:{'#dcfce7' if e['type_ecriture']=='recette' else '#fee2e2'};color:{clr};">{'💰 Recette' if e['type_ecriture']=='recette' else '💸 Dépense'}</span></td>
          <td style="font-size:11px;color:#64748b;">{e['categorie'] or '—'}</td>
          <td style="font-size:12px;font-weight:600;">{e['libelle'] or '—'}</td>
          <td style="font-size:11px;color:#64748b;">{mode}</td>
          <td style="font-size:10px;color:#94a3b8;">{src}</td>
          <td style="text-align:right;font-weight:800;color:{clr};">{sign}{fcfa(e['montant'])} {devise}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>Rapport Comptable {annee} — {nom_soc}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; font-size: 12px; color: #0f172a; background: white; padding: 28px 32px; }}
  h1 {{ font-size: 22px; font-weight: 800; color: #1a3a6c; }}
  h2 {{ font-size: 15px; font-weight: 700; color: #1a3a6c; margin: 24px 0 10px; border-left: 4px solid #2563eb; padding-left: 10px; }}
  h3 {{ font-size: 12px; font-weight: 700; color: #475569; margin-bottom: 8px; text-transform: uppercase; letter-spacing: .5px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 8px; }}
  thead th {{ background: #1a3a6c; color: white; padding: 8px 12px; text-align: left; font-size: 11px; font-weight: 700; }}
  tbody td {{ padding: 7px 12px; font-size: 12px; border-bottom: 1px solid #f1f5f9; }}
  tbody tr:hover {{ background: #f8fafc; }}
  .header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 24px; padding-bottom: 18px; border-bottom: 2px solid #1a3a6c; }}
  .logo {{ font-size: 28px; font-weight: 900; color: #1a3a6c; }}
  .logo .sub {{ font-size: 11px; font-weight: 500; color: #64748b; margin-top: 2px; }}
  .report-info {{ text-align: right; font-size: 11px; color: #64748b; }}
  .report-info .title {{ font-size: 16px; font-weight: 800; color: #1a3a6c; margin-bottom: 4px; }}
  .kpi-row {{ display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 12px; margin-bottom: 20px; }}
  .kpi {{ border: 1.5px solid #e2e8f0; border-radius: 10px; padding: 14px 16px; background: #f8fafc; }}
  .kpi .lbl {{ font-size: 10px; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: .5px; margin-bottom: 4px; }}
  .kpi .val {{ font-size: 18px; font-weight: 900; font-variant-numeric: tabular-nums; }}
  .kpi .sub {{ font-size: 10px; color: #94a3b8; margin-top: 3px; }}
  .kpi.vert {{ border-color: #bbf7d0; background: #f0fdf4; }}
  .kpi.rouge {{ border-color: #fecaca; background: #fff5f5; }}
  .kpi.or {{ border-color: #fde68a; background: #fefce8; }}
  .badge-res {{ display: inline-block; padding: 4px 14px; border-radius: 20px; font-size: 12px; font-weight: 700; margin-top: 6px; }}
  .footer {{ margin-top: 32px; padding-top: 14px; border-top: 1px solid #e2e8f0; font-size: 10px; color: #94a3b8; display: flex; justify-content: space-between; }}
  @media print {{
    body {{ padding: 16px; }}
    .no-print {{ display: none; }}
    h2 {{ page-break-before: auto; }}
    table {{ page-break-inside: auto; }}
    tr {{ page-break-inside: avoid; }}
  }}
  .print-btn {{
    position: fixed; top: 20px; right: 20px; padding: 10px 22px;
    background: #1a3a6c; color: white; border: none; border-radius: 8px;
    font-size: 13px; font-weight: 700; cursor: pointer; display: flex; align-items: center; gap: 8px;
    font-family: inherit; box-shadow: 0 4px 16px rgba(26,58,108,.3); z-index: 999;
  }}
  .print-btn:hover {{ background: #2563eb; }}
</style>
</head>
<body>
<button class="print-btn no-print" onclick="window.print()">🖨️ Imprimer / PDF</button>

<div class="header">
  <div>
    <div class="logo">🛒 {_soc['nom']}</div>
    <div class="logo sub">{_soc['adresse']} &nbsp;·&nbsp; {_soc['tel']}{(' &nbsp;·&nbsp; ' + _soc['email']) if _soc['email'] else ''}</div>
    <div class="logo sub">{('NCC : ' + _soc['ncc']) if _soc['ncc'] else ''}{' &nbsp;·&nbsp; ' if _soc['ncc'] and _soc['rccm'] else ''}{('RCCM : ' + _soc['rccm']) if _soc['rccm'] else ''}</div>
  </div>
  <div class="report-info">
    <div class="title">RAPPORT COMPTABLE</div>
    <div>Exercice {annee}</div>
    <div style="margin-top:4px;">Généré le {now_str}</div>
    <div style="margin-top:4px;font-weight:700;">DISTRIGEST · STiNAUG TECHNOLOGIE</div>
  </div>
</div>

<!-- KPIs -->
<div class="kpi-row">
  <div class="kpi vert">
    <div class="lbl">Total recettes</div>
    <div class="val" style="color:#15803d;">{fcfa(total_recettes)} {devise}</div>
    <div class="sub">{totaux['nb_recettes']} opérations</div>
  </div>
  <div class="kpi rouge">
    <div class="lbl">Total dépenses</div>
    <div class="val" style="color:#dc2626;">{fcfa(total_depenses)} {devise}</div>
    <div class="sub">{totaux['nb_depenses']} opérations</div>
  </div>
  <div class="kpi {'vert' if resultat_net >= 0 else 'rouge'}">
    <div class="lbl">Résultat net</div>
    <div class="val" style="color:{'#15803d' if resultat_net >= 0 else '#dc2626'};">{'+' if resultat_net >= 0 else '−'}{fcfa(abs(resultat_net))} {devise}</div>
    <div class="sub"><span class="badge-res" style="background:{'#dcfce7' if resultat_net >= 0 else '#fee2e2'};color:{'#15803d' if resultat_net >= 0 else '#dc2626'};">{'✅ Bénéfice' if resultat_net >= 0 else '⚠️ Déficit'}</span></div>
  </div>
  <div class="kpi or">
    <div class="lbl">Marge nette</div>
    <div class="val" style="color:#92400e;">{marge_nette}%</div>
    <div class="sub">{totaux['nb_ecritures']} écritures au total</div>
  </div>
</div>

<!-- Évolution mensuelle -->
<h2>📊 Évolution mensuelle {annee}</h2>
<table>
  <thead><tr><th>Mois</th><th style="text-align:right;">Recettes</th><th style="text-align:right;">Dépenses</th><th style="text-align:right;">Résultat du mois</th><th style="text-align:right;">Solde cumulé</th></tr></thead>
  <tbody>
    {rows_mois if rows_mois else '<tr><td colspan="5" style="text-align:center;color:#94a3b8;padding:20px;">Aucune donnée pour cet exercice</td></tr>'}
    <tr style="background:#1a3a6c;color:white;font-weight:800;">
      <td>TOTAL</td>
      <td style="text-align:right;">{fcfa(total_recettes)} {devise}</td>
      <td style="text-align:right;">{fcfa(total_depenses)} {devise}</td>
      <td style="text-align:right;">{'+' if resultat_net>=0 else '−'}{fcfa(abs(resultat_net))} {devise}</td>
      <td style="text-align:right;">—</td>
    </tr>
  </tbody>
</table>

<!-- Recettes par catégorie -->
<h2>💰 Recettes par catégorie</h2>
<table>
  <thead><tr><th>Catégorie</th><th style="text-align:center;">Opérations</th><th style="text-align:right;">Montant</th><th style="text-align:right;">Part</th></tr></thead>
  <tbody>{rows_cat_rec if rows_cat_rec else '<tr><td colspan="4" style="text-align:center;color:#94a3b8;padding:16px;">Aucune recette</td></tr>'}</tbody>
</table>

<!-- Dépenses par catégorie -->
<h2>💸 Dépenses par catégorie</h2>
<table>
  <thead><tr><th>Catégorie</th><th style="text-align:center;">Opérations</th><th style="text-align:right;">Montant</th><th style="text-align:right;">Part</th></tr></thead>
  <tbody>{rows_cat_dep if rows_cat_dep else '<tr><td colspan="4" style="text-align:center;color:#94a3b8;padding:16px;">Aucune dépense</td></tr>'}</tbody>
</table>

{'<h2>🏆 Top 5 clients encaissés</h2><table><thead><tr><th>Client</th><th style="text-align:right;">Total encaissé</th></tr></thead><tbody>' + rows_clients + '</tbody></table>' if rows_clients else ''}

<!-- Journal complet -->
<h2>📋 Journal comptable complet — {annee}</h2>
<table>
  <thead><tr><th>Date</th><th>Type</th><th>Catégorie</th><th>Libellé</th><th>Mode</th><th>Source</th><th style="text-align:right;">Montant</th></tr></thead>
  <tbody>{rows_journal if rows_journal else '<tr><td colspan="7" style="text-align:center;color:#94a3b8;padding:20px;">Aucune écriture pour cet exercice</td></tr>'}</tbody>
</table>

<div class="footer">
  <span>{nom_soc} · Rapport comptable {annee} · Généré le {now_str}</span>
  <span>DISTRIGEST · STiNAUG TECHNOLOGIE · Abidjan, Côte d'Ivoire</span>
</div>
<script>window.addEventListener('load',function(){{setTimeout(function(){{window.print();}},250);}});</script>
</body>
</html>"""

    html = _inject_print_format(html, _print_format_css(_current_print_format()))
    html = _journal_print_response(html, 'document_imprime')
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/comptabilite/importer')
@login_required
def comptabilite_importer():
    nb = 0
    # Factures ventes
    for fac in query("""
        SELECT dv.id, dv.reference, dv.date_doc, dv.montant_paye, dv.mode_paiement, c.nom as client_nom
        FROM documents_vente dv LEFT JOIN clients c ON c.id=dv.client_id
        WHERE dv.type_doc='facture' AND dv.montant_paye > 0
          AND NOT EXISTS (SELECT 1 FROM ecritures_comptables ec WHERE ec.source='facture' AND ec.source_id=dv.id)
    """):
        execute("""INSERT OR IGNORE INTO ecritures_comptables(type_ecriture,date_ecriture,categorie,libelle,montant,mode_paiement,source,source_id)
                   VALUES('recette',?,?,?,?,?,'facture',?)""",
                (fac['date_doc'], 'Ventes',
                 'Facture ' + str(fac['reference']) + ' — ' + str(fac['client_nom'] or 'Client'),
                 fac['montant_paye'], fac['mode_paiement'] or 'especes', fac['id']))
        nb += 1
    # Règlements LIBRES uniquement (sans facture liée).
    #  • Les règlements liés à une facture/vente client sont déjà comptabilisés
    #    via la boucle « Factures ventes » (montant_paye) → ne pas redoubler.
    #  • Les règlements fournisseurs (source_type='facture_fourn') sont traités
    #    plus bas par la boucle « Règlements fournisseurs » (en dépense).
    for reg in query("""
        SELECT r.id, r.date_reglement, r.montant, r.mode_paiement, c.nom as client_nom
        FROM reglements r LEFT JOIN clients c ON c.id=r.client_id
        WHERE r.client_id IS NOT NULL AND r.fournisseur_id IS NULL
          AND (r.source_type IS NULL OR r.source_type NOT IN ('facture','vente','facture_fourn'))
          AND NOT EXISTS (SELECT 1 FROM ecritures_comptables ec WHERE ec.source='reglement' AND ec.source_id=r.id)
    """):
        execute("""INSERT OR IGNORE INTO ecritures_comptables(type_ecriture,date_ecriture,categorie,libelle,montant,mode_paiement,source,source_id)
                   VALUES('recette',?,?,?,?,?,'reglement',?)""",
                (reg['date_reglement'], 'Reglements clients',
                 'Reglement — ' + str(reg['client_nom'] or 'Client'),
                 reg['montant'], reg['mode_paiement'] or 'especes', reg['id']))
        nb += 1
    # Depenses
    for dep in query("""
        SELECT id, date_depense, categorie, description, montant FROM depenses
        WHERE NOT EXISTS (SELECT 1 FROM ecritures_comptables ec WHERE ec.source='depense' AND ec.source_id=id)
    """):
        execute("""INSERT OR IGNORE INTO ecritures_comptables(type_ecriture,date_ecriture,categorie,libelle,montant,source,source_id)
                   VALUES('depense',?,?,?,?,'depense',?)""",
                (dep['date_depense'], dep['categorie'], dep['description'], dep['montant'], dep['id']))
        nb += 1
    # Règlements de commandes d'achat (paiements réels — comptabilité de caisse)
    #  • On ne comptabilise PLUS le TTC complet du document (= engagement), mais
    #    uniquement les paiements effectifs saisis via le bouton « Régler »
    #    (table reglements, source_type='achat'). Cela évite le double comptage
    #    avec la vue /comptabilite, qui lit déjà ces règlements en dépense
    #    (sql_dep_fourn, fournisseur_id renseigné).
    for rga in query("""
        SELECT r.id, r.date_reglement, r.montant, r.mode_paiement, r.reference,
               f.nom as fourn_nom
        FROM reglements r
        LEFT JOIN fournisseurs f ON f.id = r.fournisseur_id
        WHERE r.source_type = 'achat'
          AND NOT EXISTS (SELECT 1 FROM ecritures_comptables ec
                          WHERE ec.source='reglement_achat' AND ec.source_id=r.id)
    """):
        execute("""INSERT OR IGNORE INTO ecritures_comptables
                   (type_ecriture,date_ecriture,categorie,libelle,montant,mode_paiement,source,source_id)
                   VALUES('depense',?,?,?,?,?,'reglement_achat',?)""",
                (rga['date_reglement'],
                 'Achats',
                 'Paiement achat ' + str(rga['reference']) + ' — ' + str(rga['fourn_nom'] or 'Fournisseur'),
                 rga['montant'],
                 rga['mode_paiement'] or 'virement',
                 rga['id']))
        nb += 1
    # ── Paies payées ──────────────────────────────────────────────────
    for fp in query("""
        SELECT fp.id, fp.mois, fp.salaire_net, fp.salaire_brut,
               fp.mode_paiement, fp.date_paiement,
               e.nom, e.prenom
        FROM fiches_paie fp JOIN employes e ON e.id=fp.employe_id
        WHERE fp.statut='paye'
          AND NOT EXISTS (SELECT 1 FROM ecritures_comptables ec WHERE ec.source='paie' AND ec.source_id=fp.id)
    """):
        nom_emp = f"{fp['nom']} {fp['prenom'] or ''}".strip()
        execute("""INSERT OR IGNORE INTO ecritures_comptables
                   (type_ecriture,date_ecriture,categorie,libelle,montant,mode_paiement,source,source_id,notes)
                   VALUES('depense',?,?,?,?,?,'paie',?,?)""",
                (fp['date_paiement'] or date.today().isoformat(),
                 'Personnel',
                 f"Salaire {nom_emp} — {fp['mois']}",
                 fp['salaire_net'],
                 fp['mode_paiement'] or 'especes',
                 fp['id'],
                 f"Brut: {fp['salaire_brut']} — Net: {fp['salaire_net']}"))
        nb += 1

    # ── Factures fournisseurs (payées ou partielles) ───────────────────
    for ff in query("""
        SELECT ff.id, ff.reference, ff.date_facture, ff.montant_paye, ff.total_ttc,
               ff.mode_paiement, f.nom as fourn_nom
        FROM factures_fournisseurs ff
        LEFT JOIN fournisseurs f ON f.id = ff.fournisseur_id
        WHERE ff.montant_paye > 0
          AND NOT EXISTS (SELECT 1 FROM ecritures_comptables ec
                          WHERE ec.source='facture_fourn' AND ec.source_id=ff.id)
    """):
        execute("""INSERT OR IGNORE INTO ecritures_comptables
                   (type_ecriture,date_ecriture,categorie,libelle,montant,mode_paiement,source,source_id)
                   VALUES('depense',?,?,?,?,?,'facture_fourn',?)""",
                (ff['date_facture'],
                 'Achats',
                 'Facture fourn. ' + str(ff['reference']) + ' — ' + str(ff['fourn_nom'] or 'Fournisseur'),
                 ff['montant_paye'],
                 ff['mode_paiement'] or 'virement',
                 ff['id']))
        nb += 1

    # ── Avoirs clients (remboursements effectifs en espèces/virement) ──
    for av in query("""
        SELECT av.id, av.reference, av.date_avoir, av.total_ttc, av.mode_remboursement,
               av.motif, c.nom as client_nom
        FROM avoirs_clients av
        LEFT JOIN clients c ON c.id = av.client_id
        WHERE COALESCE(av.total_ttc,0) > 0
          AND NOT EXISTS (SELECT 1 FROM ecritures_comptables ec
                          WHERE ec.source='avoir_client' AND ec.source_id=av.id)
    """):
        execute("""INSERT OR IGNORE INTO ecritures_comptables
                   (type_ecriture,date_ecriture,categorie,libelle,montant,mode_paiement,source,source_id,notes)
                   VALUES('depense',?,?,?,?,?,'avoir_client',?,?)""",
                (av['date_avoir'],
                 'Avoirs clients',
                 'Avoir ' + str(av['reference']) + ' — ' + str(av['client_nom'] or 'Client'),
                 av['total_ttc'],
                 av['mode_remboursement'] or 'credit',
                 av['id'],
                 av['motif'] or ''))
        nb += 1

    # ── Avoirs fournisseurs (créances reçues = recette) ────────────────
    for avf in query("""
        SELECT avf.id, avf.reference, avf.date_avoir, avf.total_ttc, avf.motif,
               f.nom as fourn_nom
        FROM avoirs_fournisseurs avf
        LEFT JOIN fournisseurs f ON f.id = avf.fournisseur_id
        WHERE COALESCE(avf.total_ttc,0) > 0
          AND NOT EXISTS (SELECT 1 FROM ecritures_comptables ec
                          WHERE ec.source='avoir_fourn' AND ec.source_id=avf.id)
    """):
        execute("""INSERT OR IGNORE INTO ecritures_comptables
                   (type_ecriture,date_ecriture,categorie,libelle,montant,source,source_id,notes)
                   VALUES('recette',?,?,?,?,'avoir_fourn',?,?)""",
                (avf['date_avoir'],
                 'Avoirs fournisseurs',
                 'Avoir fourn. ' + str(avf['reference']) + ' — ' + str(avf['fourn_nom'] or 'Fournisseur'),
                 avf['total_ttc'],
                 avf['id'],
                 avf['motif'] or ''))
        nb += 1

    # ── Règlements fournisseurs (paiements directs depuis table reglements) ──
    for rff in query("""
        SELECT r.id, r.date_reglement, r.montant, r.mode_paiement, r.reference,
               f.nom as fourn_nom
        FROM reglements r
        LEFT JOIN fournisseurs f ON f.id = r.fournisseur_id
        WHERE r.source_type = 'facture_fourn'
          AND NOT EXISTS (SELECT 1 FROM ecritures_comptables ec
                          WHERE ec.source='reglement_fourn' AND ec.source_id=r.id)
    """):
        execute("""INSERT OR IGNORE INTO ecritures_comptables
                   (type_ecriture,date_ecriture,categorie,libelle,montant,mode_paiement,source,source_id)
                   VALUES('depense',?,?,?,?,?,'reglement_fourn',?)""",
                (rff['date_reglement'],
                 'Règlements fournisseurs',
                 'Paiement fourn. ' + str(rff['reference']) + ' — ' + str(rff['fourn_nom'] or 'Fournisseur'),
                 rff['montant'],
                 rff['mode_paiement'] or 'virement',
                 rff['id']))
        nb += 1

    # ── Commissions représentants payées ──────────────────────────────
    for com in query("""
        SELECT com.id, com.date_paiement, com.montant_commission, com.taux,
               com.montant_base, r.nom, r.prenom
        FROM commissions com
        JOIN representants r ON r.id = com.representant_id
        WHERE com.statut = 'payee'
          AND NOT EXISTS (SELECT 1 FROM ecritures_comptables ec
                          WHERE ec.source='commission' AND ec.source_id=com.id)
    """):
        nom_rep = f"{com['nom']} {com['prenom'] or ''}".strip()
        execute("""INSERT OR IGNORE INTO ecritures_comptables
                   (type_ecriture,date_ecriture,categorie,libelle,montant,source,source_id,notes)
                   VALUES('depense',?,?,?,?,'commission',?,?)""",
                (com['date_paiement'] or date.today().isoformat(),
                 'Commissions',
                 f"Commission {nom_rep} — {com['taux']}%",
                 com['montant_commission'],
                 com['id'],
                 f"Base: {com['montant_base']} — Taux: {com['taux']}%"))
        nb += 1

    flash(f"Import terminé : {nb} écriture(s) ajoutée(s).", 'success')
    return redirect(url_for('comptabilite'))


@app.route('/ecritures/add', methods=['POST'])
@login_required
def ecriture_add():
    """Saisie manuelle d'une écriture comptable (ajustement, apport, cession, etc.).
       Les écritures automatiques (règlements, dépenses) sont prises depuis leurs tables sources."""
    frm = request.form

    # ── Validation ────────────────────────────────────────────
    type_ec = (frm.get('type_ecriture') or '').strip()
    if type_ec not in ('recette', 'depense'):
        flash("Type d'écriture invalide (recette ou dépense).", "danger")
        return redirect(url_for('comptabilite'))

    date_ec = (frm.get('date_ecriture') or '').strip()
    if not date_ec:
        date_ec = date.today().isoformat()

    libelle = (frm.get('libelle') or '').strip()
    if not libelle:
        flash("Le libellé est obligatoire.", "danger")
        return redirect(url_for('comptabilite'))

    categorie = (frm.get('categorie') or 'Autre').strip() or 'Autre'

    try:
        montant = float(frm.get('montant', 0) or 0)
    except (ValueError, TypeError):
        montant = 0
    if montant <= 0:
        flash("Le montant doit être supérieur à zéro.", "danger")
        return redirect(url_for('comptabilite'))

    mode = (frm.get('mode_paiement') or 'especes').strip() or 'especes'
    notes = (frm.get('notes') or '').strip()
    motif = (frm.get('motif') or '').strip()

    # ── Insertion (source = NULL → écriture manuelle, supprimable) ─
    execute("""INSERT INTO ecritures_comptables
               (type_ecriture, date_ecriture, categorie, libelle, montant,
                mode_paiement, source, source_id, notes, motif)
               VALUES (?,?,?,?,?,?,NULL,NULL,?,?)""",
            (type_ec, date_ec, categorie, libelle, round(montant, 2),
             mode, notes, motif))
    flash(f"Écriture {('+' if type_ec=='recette' else '−')}{int(montant):,} FCFA enregistrée.".replace(',', ' '), "success")
    return redirect(url_for('comptabilite', annee=date_ec[:4]))


@app.route('/ecritures/delete/<int:id>')
@login_required
def ecriture_delete(id):
    """Supprimer une écriture MANUELLE uniquement (source IS NULL).
       Les écritures auto (règlements, dépenses) doivent être supprimées depuis leur source."""
    row = query("SELECT id, libelle, source FROM ecritures_comptables WHERE id=?", (id,), one=True)
    if not row:
        flash("Écriture introuvable.", "warning")
    elif row['source']:
        flash("Cette écriture est générée automatiquement. Supprimez-la depuis sa source (règlement ou dépense).", "warning")
    else:
        execute("DELETE FROM ecritures_comptables WHERE id=? AND (source IS NULL OR source='')", (id,))
        flash("Écriture supprimée.", "success")
    return redirect(url_for('comptabilite'))



@app.route('/clients/<int:id>/edit', methods=['POST'])
@login_required
def client_edit_full(id):
    f = request.form
    # ── Code client : contrôle d'unicité (si modifié) ───────────────────
    code_nouv = (f.get('code') or '').strip() or None
    if code_nouv:
        conflit = query("SELECT id FROM clients WHERE code=? AND id<>?",
                        (code_nouv, id), one=True)
        if conflit:
            flash(f"Code « {code_nouv} » déjà utilisé par un autre client.", "danger")
            return redirect(url_for('fiche_client', id=id))
    # ── Statut actif : 1 par défaut si valeur absente ou invalide ──────
    try:
        actif_v = 1 if int(f.get('actif', 1)) else 0
    except (TypeError, ValueError):
        actif_v = 1
    execute("""UPDATE clients SET
        code=?, actif=?,
        nom=?, prenom=?, type_client=?, telephone=?, telephone_fixe=?,
        telephone2=?, email=?, adresse=?, ville=?, zone_livraison=?,
        encours_autorise=?, plafond_credit=?, remise_pct=?,
        mode_paiement=?, delai_paiement=?,
        matricule_fiscal=?, code_comptable=?,
        secteur=?, categorie_client=?,
        responsable_compte=?, site_web=?, notes=?
        WHERE id=?""",
        (code_nouv, actif_v,
         f['nom'].upper(), f.get('prenom'), f.get('type_client','particulier'),
         f.get('telephone'), f.get('telephone_fixe'),
         f.get('telephone2'), f.get('email'), f.get('adresse'),
         f.get('ville','Abidjan'), f.get('zone_livraison'),
         float(f.get('encours_autorise',0) or 0),
         float(f.get('plafond_credit',0) or 0),
         float(f.get('remise_pct',0) or 0),
         f.get('mode_paiement','especes'),
         int(f.get('delai_paiement',0) or 0),
         f.get('matricule_fiscal'), f.get('code_comptable'),
         f.get('secteur'), f.get('categorie_client','standard'),
         f.get('responsable_compte'), f.get('site_web'), f.get('notes'),
         id))
    flash("Fiche client mise à jour.", "success")
    return redirect(url_for('fiche_client', id=id))


@app.route('/fournisseurs/<int:id>/edit', methods=['POST'])
@login_required
def fournisseur_edit_full(id):
    f = request.form
    # ── Code fournisseur : contrôle d'unicité (si modifié) ──────────────
    code_nouv = (f.get('code') or '').strip() or None
    if code_nouv:
        conflit = query("SELECT id FROM fournisseurs WHERE code=? AND id<>?",
                        (code_nouv, id), one=True)
        if conflit:
            flash(f"Code « {code_nouv} » déjà utilisé par un autre fournisseur.", "danger")
            return redirect(url_for('fiche_fournisseur', id=id))
    # ── Statut actif : 1 par défaut si valeur absente ou invalide ──────
    try:
        actif_v = 1 if int(f.get('actif', 1)) else 0
    except (TypeError, ValueError):
        actif_v = 1
    execute("""UPDATE fournisseurs SET
        code=?, actif=?,
        nom=?, contact=?, contact2=?, telephone=?, telephone_fixe=?,
        telephone2=?, email=?, adresse=?, ville=?, pays=?,
        type_produits=?, delai_livraison=?, conditions_paiement=?,
        remise_pct=?, plafond_credit=?,
        matricule_fiscal=?, code_comptable=?,
        secteur=?, categorie_fournisseur=?,
        site_web=?, notes=?
        WHERE id=?""",
        (code_nouv, actif_v,
         f['nom'].upper(), f.get('contact'), f.get('contact2'),
         f.get('telephone'), f.get('telephone_fixe'),
         f.get('telephone2'), f.get('email'), f.get('adresse'),
         f.get('ville','Abidjan'), f.get('pays',"Côte d'Ivoire"),
         f.get('type_produits'), f.get('delai_livraison'), f.get('conditions_paiement'),
         float(f.get('remise_pct',0) or 0),
         float(f.get('plafond_credit',0) or 0),
         f.get('matricule_fiscal'), f.get('code_comptable'),
         f.get('secteur'), f.get('categorie_fournisseur','standard'),
         f.get('site_web'), f.get('notes'),
         id))
    flash("Fiche fournisseur mise à jour.", "success")
    return redirect(url_for('fiche_fournisseur', id=id))


# ══════════════════════════════════════════════════════════════════════
#  PAIEMENT DIRECT D'UNE COMMANDE D'ACHAT
# ══════════════════════════════════════════════════════════════════════
@app.route('/achats/<int:id>/payer', methods=['POST'])
@login_required
def achat_payer(id):
    """Enregistre un règlement direct sur une commande d'achat (documents_achat).
    Met à jour montant_paye / reste / statut du document et crée une ligne
    dans 'reglements' avec source_type='achat' et fournisseur_id renseigné.
    """
    f = request.form
    redirect_to = f.get('redirect_url') or None
    doc = query("SELECT * FROM documents_achat WHERE id=?", (id,), one=True)
    if not doc:
        flash("Commande d'achat introuvable.", "danger")
        return redirect(url_for('achats_list') if 'achats_list' in app.view_functions else url_for('index'))

    try:
        montant = float(f.get('montant') or 0)
    except (TypeError, ValueError):
        montant = 0
    if montant <= 0:
        flash("Montant de paiement invalide.", "danger")
        return redirect(redirect_to or url_for('fiche_fournisseur', id=doc['fournisseur_id']))

    mode      = f.get('mode_paiement', 'virement')
    date_rgl  = f.get('date_reglement') or date.today().isoformat()
    notes     = f.get('notes', '')

    # Mise à jour document_achat
    new_paye  = round((doc['montant_paye'] or 0) + montant, 2)
    new_reste = round(max(0, (doc['total_ttc'] or 0) - new_paye), 2)
    # Le statut métier (en_attente / partiellement_recu / recu) reflète la livraison,
    # pas le paiement. On ne le modifie donc pas ici (seuls montant_paye/reste évoluent).
    execute("""UPDATE documents_achat SET montant_paye=?, reste=?
               WHERE id=?""",
            (new_paye, new_reste, id))

    # Insertion règlement (source_type='achat')
    ref = next_ref_rgl()
    execute("""INSERT INTO reglements
               (reference, source_type, source_id, fournisseur_id,
                montant, mode_paiement, date_reglement, notes)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (ref, 'achat', id, doc['fournisseur_id'],
             montant, mode, date_rgl, notes))

    flash(f"Paiement {ref} enregistré — {montant:,.0f} FCFA.".replace(',', ' '),
          "success")
    return redirect(redirect_to or url_for('fiche_fournisseur', id=doc['fournisseur_id']))


# ── Changer le statut d'une commande d'achat ──────────────────────────
@app.route('/achats/<int:id>/statut', methods=['POST'])
@login_required
def achat_statut(id):
    """Met à jour le statut métier d'une commande d'achat (menu déroulant)."""
    statuts_ok = {'en_attente', 'confirmee', 'recu', 'annule'}
    new_statut = request.form.get('statut', '')
    if new_statut not in statuts_ok:
        flash("Statut invalide.", "danger")
        return redirect(url_for('achats_list'))
    doc = query("SELECT id, statut FROM documents_achat WHERE id=? AND type_doc='commande'", (id,), one=True)
    if not doc:
        flash("Commande d'achat introuvable.", "danger")
        return redirect(url_for('achats_list'))
    # Une commande reçue (ou facturée/convertie) est verrouillée :
    # plus aucun changement de statut possible, y compris l'annulation.
    if doc['statut'] in ('recu', 'facturee', 'converti'):
        flash("Commande reçue : statut verrouillé, modification impossible.", "warning")
        return redirect(url_for('achats_list'))
    execute("UPDATE documents_achat SET statut=? WHERE id=?", (new_statut, id))
    flash("Statut de la commande mis à jour.", "success")
    return redirect(url_for('achats_list'))



# ══════════════════════════════════════════════════════════════════════
#  CYCLE DE VENTE COMPLET
# ══════════════════════════════════════════════════════════════════════

# ── Convertir devis → commande ──────────────────────────────────────
@app.route('/devis/<int:id>/convertir_commande', methods=['POST'])
@login_required
def devis_convertir_commande(id):
    devis = query("SELECT * FROM documents_vente WHERE id=? AND type_doc='devis'", (id,), one=True)
    if not devis:
        flash("Devis introuvable.", "danger")
        return redirect(url_for('devis_list'))
    if devis['statut'] == 'converti':
        flash("Ce devis a déjà été converti en commande.", "warning")
        return redirect(url_for('devis_list'))
    # Numérotation mensuelle cohérente avec le reste de l'application
    ref = next_ref('CMD')
    execute("""INSERT INTO documents_vente
               (type_doc,reference,client_id,depot_id,date_doc,date_livraison,date_echeance,
                remise_globale,total_ht,total_tva,total_ttc,reste,mode_paiement,notes,doc_parent_id,statut)
               VALUES('commande',?,?,?,date('now'),?,?,?,?,?,?,?,?,?,?,'en_attente')""",
            (ref, devis['client_id'], devis['depot_id'],
             devis['date_livraison'], devis['date_echeance'],
             devis['remise_globale'], devis['total_ht'], devis['total_tva'], devis['total_ttc'],
             devis['total_ttc'],   # reste = total_ttc à la création
             devis['mode_paiement'], devis['notes'], id))
    new_id = query("SELECT id FROM documents_vente WHERE reference=?", (ref,), one=True)['id']
    # Copier les lignes
    lignes = query("SELECT * FROM lignes_vente WHERE document_id=?", (id,))
    for l in lignes:
        execute("""INSERT INTO lignes_vente
                   (document_id,article_id,designation,quantite_unite,quantite_colis,
                    prix_ht,remise_pct,tva,total_ht,total_ttc,num_ligne)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id, l['article_id'], l['designation'],
                 l['quantite_unite'], l['quantite_colis'],
                 l['prix_ht'], l['remise_pct'], l['tva'],
                 l['total_ht'], l['total_ttc'], l['num_ligne']))
    # Archiver le devis → statut 'converti'
    execute("UPDATE documents_vente SET statut='converti' WHERE id=?", (id,))
    flash(f"Devis {devis['reference']} converti en commande {ref}.", "success")
    return redirect(url_for('commandes_vente_list'))


# ── Convertir commande → BL ──────────────────────────────────────────
# ── Convertir commande vente → Facture directe (sans BL) ───────────────
@app.route('/commandes/<int:id>/convertir_facture', methods=['POST'])
@login_required
def commande_convertir_facture(id):
    cmd = query("SELECT * FROM documents_vente WHERE id=? AND type_doc='commande'", (id,), one=True)
    if not cmd:
        flash("Commande introuvable.", "danger")
        return redirect(url_for('commandes_vente_list'))
    # Bloquer si un BL existe déjà (il faut passer par BL → Facturer)
    if cmd['statut'] == 'bl_cree':
        bl_exist = query("SELECT reference FROM bons_livraison WHERE commande_id=? AND statut != 'annule'", (id,), one=True)
        ref_bl = bl_exist['reference'] if bl_exist else 'BL existant'
        flash(f"Un bon de livraison ({ref_bl}) a été créé pour cette commande. "
              f"Utilisez le bouton 🧾 Facturer depuis la page Bons de Livraison.", "warning")
        return redirect(url_for('bons_livraison_list'))
    # Bloquer si déjà facturée ou annulée
    if cmd['statut'] in ('facturee', 'annulee'):
        flash("Cette commande est déjà facturée ou annulée.", "warning")
        return redirect(url_for('commandes_vente_list'))
    # Vérifier qu'une facture directe n'existe pas déjà
    exist = query("SELECT id FROM documents_vente WHERE doc_parent_id=? AND type_doc='facture'", (id,), one=True)
    if exist:
        flash("Une facture existe déjà pour cette commande.", "warning")
        return redirect(url_for('factures_list'))
    # Numérotation mensuelle cohérente
    ref = next_ref('FAC')
    execute("""INSERT INTO documents_vente
               (type_doc,reference,client_id,depot_id,date_doc,date_echeance,
                remise_globale,total_ht,total_tva,total_ttc,reste,
                mode_paiement,notes,doc_parent_id,statut)
               VALUES('facture',?,?,?,date('now'),?,?,?,?,?,?,?,?,?,'en_attente')""",
            (ref, cmd['client_id'], cmd['depot_id'],
             cmd['date_echeance'], cmd['remise_globale'],
             cmd['total_ht'], cmd['total_tva'], cmd['total_ttc'], cmd['total_ttc'],
             cmd['mode_paiement'], cmd['notes'], id))
    fac_id = query("SELECT id FROM documents_vente WHERE reference=?", (ref,), one=True)['id']
    lignes = query("SELECT * FROM lignes_vente WHERE document_id=?", (id,))
    for l in lignes:
        execute("""INSERT INTO lignes_vente
                   (document_id,article_id,designation,quantite_unite,quantite_colis,
                    prix_ht,remise_pct,tva,total_ht,total_ttc,num_ligne)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (fac_id, l['article_id'], l['designation'],
                 l['quantite_unite'], l['quantite_colis'],
                 l['prix_ht'], l['remise_pct'], l['tva'],
                 l['total_ht'], l['total_ttc'], l['num_ligne']))
    # ── Décrémenter le stock à la création de la facture directe ──────────
    dep_id = resolve_depot_id(cmd['depot_id'])
    for l in lignes:
        if l['article_id']:
            qte = float(l['quantite_unite'] or 0)
            if qte > 0:
                execute("""UPDATE stocks SET quantite_unite=MAX(0, quantite_unite-?)
                           WHERE article_id=? AND depot_id=?""",
                        (qte, l['article_id'], dep_id))
                execute("""INSERT INTO mouvements_stocks
                           (article_id,depot_id,type_mvt,quantite_unite,doc_type,doc_id,doc_ref,operateur)
                           VALUES(?,?,'sortie',?,?,?,?,?)""",
                        (l['article_id'], dep_id, qte,
                         'facture', fac_id, ref, session.get('user_nom', '')))
    execute("UPDATE documents_vente SET statut='facturee' WHERE id=?", (id,))
    flash(f"Facture {ref} générée depuis la commande {cmd['reference']}.", "success")
    return redirect(url_for('factures_list'))


# ── Convertir facture vente → Avoir client ────────────────────────────
@app.route('/factures/<int:id>/creer_avoir', methods=['POST'])
@login_required
def facture_creer_avoir(id):
    fac = query("SELECT * FROM documents_vente WHERE id=? AND type_doc='facture'", (id,), one=True)
    if not fac:
        flash("Facture introuvable.", "danger")
        return redirect(url_for('factures_list'))
    n = query("SELECT COUNT(*) as c FROM avoirs_clients", one=True)['c']
    ref = "AV{:05d}".format(n + 1)
    ht  = fac['total_ht'] or 0
    tva = fac['total_tva'] or 0
    ttc = fac['total_ttc'] or 0
    motif = request.form.get('motif', 'Avoir sur facture ' + fac['reference'])
    type_avoir = request.form.get('type_avoir', 'retour')
    mode_remb  = request.form.get('mode_remboursement', 'credit')
    execute("""INSERT INTO avoirs_clients
               (reference,facture_id,client_id,date_avoir,motif,type_avoir,
                total_ht,total_tva,total_ttc,mode_remboursement,notes,statut)
               VALUES(?,?,?,date('now'),?,?,?,?,?,?,?,'en_attente')""",
            (ref, id, fac['client_id'], motif, type_avoir,
             round(ht,2), round(tva,2), round(ttc,2), mode_remb,
             'Avoir généré depuis facture ' + fac['reference']))
    execute("UPDATE documents_vente SET statut='avoir_emis' WHERE id=?", (id,))
    flash("Avoir " + ref + " créé depuis la facture " + fac['reference'] + ".", "success")
    return redirect(url_for('avoirs_list'))








@app.route('/commandes/<int:id>/lier_reglement', methods=['POST'])
@login_required
def commande_lier_reglement(id):
    """Lie un règlement libre (avance) à une commande non-livrée."""
    cmd = query("SELECT * FROM documents_vente WHERE id=? AND type_doc='commande'", (id,), one=True)
    if not cmd:
        flash("Commande introuvable.", "danger")
        return redirect(url_for('commandes_vente_list'))

    rgl_id = int(request.form.get('reglement_id', 0) or 0)
    if not rgl_id:
        flash("Aucun règlement sélectionné.", "warning")
        return redirect(url_for('commandes_vente_list'))

    rgl = query("SELECT * FROM reglements WHERE id=?", (rgl_id,), one=True)
    if not rgl:
        flash("Règlement introuvable.", "warning")
        return redirect(url_for('commandes_vente_list'))

    # Vérifier que le règlement n'est pas déjà lié à une commande ou facture
    if rgl['source_type'] not in ('libre', 'commande'):
        flash("Ce règlement est déjà imputé sur une facture.", "warning")
        return redirect(url_for('commandes_vente_list'))

    # Lier le règlement à cette commande
    execute("""UPDATE reglements
               SET source_type='commande', source_id=?, commande_id=?
               WHERE id=?""",
            (id, id, rgl_id))

    flash(f"Règlement {rgl['reference']} lié à la commande {cmd['reference']}.", "success")
    return redirect(url_for('commandes_vente_list'))

@app.route('/commandes/<int:id>/creer_bl', methods=['POST'])
@login_required
def commande_creer_bl(id):
    cmd = query("""SELECT dv.*, c.nom as client_nom
                   FROM documents_vente dv
                   LEFT JOIN clients c ON c.id=dv.client_id
                   WHERE dv.id=? AND dv.type_doc='commande'""", (id,), one=True)
    if not cmd:
        flash("Commande introuvable.", "danger")
        return redirect(url_for('commandes_vente_list'))
    # Bloquer si déjà facturée ou annulée
    if cmd['statut'] in ('facturee', 'annulee'):
        flash("Cette commande est déjà facturée ou annulée — impossible de créer un BL.", "warning")
        return redirect(url_for('commandes_vente_list'))
    # Empêcher la création d'un deuxième BL pour la même commande
    bl_exist = query("SELECT id, reference FROM bons_livraison WHERE commande_id=? AND statut != 'annule'", (id,), one=True)
    if bl_exist:
        flash(f"Un bon de livraison ({bl_exist['reference']}) existe déjà pour cette commande.", "warning")
        return redirect(url_for('bons_livraison_list'))
    # Numérotation mensuelle depuis la table bons_livraison (évite les doublons)
    ref = next_ref_bl()
    # Totaux issus directement de la commande (remises et TVA déjà calculées)
    total_ht_bl  = cmd['total_ht']  or 0
    total_tva_bl = cmd['total_tva'] or 0
    total_ttc_bl = cmd['total_ttc'] or 0
    execute("""INSERT INTO bons_livraison
               (reference,commande_id,client_id,depot_id,date_bl,date_livraison,
                statut,livreur,notes,total_ht,total_tva,total_ttc)
               VALUES(?,?,?,?,date('now'),?,'brouillon',?,?,?,?,?)""",
            (ref, id, cmd['client_id'], cmd['depot_id'],
             request.form.get('date_livraison', date.today().isoformat()),
             request.form.get('livreur',''), cmd['notes'],
             round(total_ht_bl, 2), round(total_tva_bl, 2), round(total_ttc_bl, 2)))
    bl_id = query("SELECT id FROM bons_livraison WHERE reference=?", (ref,), one=True)['id']
    lignes = query("SELECT * FROM lignes_vente WHERE document_id=?", (id,))
    for l in lignes:
        execute("""INSERT INTO lignes_bl
                   (bl_id,article_id,designation,quantite_commandee,quantite_livree,
                    quantite_colis,prix_ht,remise_pct,tva,total_ht,total_ttc,num_ligne)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (bl_id, l['article_id'], l['designation'],
                 l['quantite_unite'] or 0, l['quantite_unite'] or 0,
                 (l['quantite_colis'] if 'quantite_colis' in l.keys() else 0) or 0,
                 l['prix_ht'], l['remise_pct'] or 0, l['tva'],
                 l['total_ht'], l['total_ttc'], l['num_ligne']))
    execute("UPDATE documents_vente SET statut='bl_cree' WHERE id=?", (id,))
    flash(f"Bon de livraison {ref} créé pour {cmd['client_nom'] or 'Client occ.'}.", "success")
    return redirect(url_for('bons_livraison_list'))


# ── Liste BL ─────────────────────────────────────────────────────────
@app.route('/bons_livraison')
@login_required
def bons_livraison_list():
    cfg = get_cfg()
    q = request.args.get('q','')
    statut_f = request.args.get('statut','')
    sql = """SELECT bl.*, c.nom as client_nom, d.nom as depot_nom
             FROM bons_livraison bl
             LEFT JOIN clients c ON c.id=bl.client_id
             LEFT JOIN depots d ON d.id=bl.depot_id
             WHERE 1=1"""
    args = []
    if q:
        sql += " AND (bl.reference LIKE ? OR c.nom LIKE ?)"; args += [f'%{q}%',f'%{q}%']
    if statut_f:
        sql += " AND bl.statut=?"; args.append(statut_f)
    sql += " ORDER BY bl.date_creation DESC"
    bls = query(sql, args)
    stats = query("""SELECT
        COUNT(*) as total,
        SUM(CASE WHEN statut='brouillon' THEN 1 ELSE 0 END) as brouillons,
        SUM(CASE WHEN statut='livre' THEN 1 ELSE 0 END) as livres,
        SUM(CASE WHEN statut='facture' THEN 1 ELSE 0 END) as factures
        FROM bons_livraison""", one=True)

    # Liste des livreurs actifs (chauffeurs en priorité, puis caristes/manutentionnaires en secours)
    livreurs = query("""SELECT id, nom, prenom, poste, telephone
                        FROM employes
                        WHERE statut='actif'
                          AND poste IN ('Chauffeur livreur','Cariste','Manutentionnaire')
                        ORDER BY
                          CASE poste
                            WHEN 'Chauffeur livreur' THEN 1
                            WHEN 'Cariste' THEN 2
                            ELSE 3
                          END,
                          nom, prenom""")

    return render_template('bons_livraison.html', cfg=cfg, bls=bls, stats=stats,
                           q=q, statut_f=statut_f, livreurs=livreurs)


@app.route('/bons_livraison/<int:id>/livrer', methods=['POST'])
@login_required
def bl_livrer(id):
    bl = query("SELECT * FROM bons_livraison WHERE id=?", (id,), one=True)
    if not bl:
        flash("BL introuvable.", "danger")
        return redirect(url_for('bons_livraison_list'))

    # Récupération des champs du modal
    livreur        = (request.form.get('livreur') or '').strip()
    date_livraison = (request.form.get('date_livraison') or '').strip() or None

    # NE PAS modifier les lignes BL ici : quantite_livree et total_ttc sont
    # fixés à la création du BL et ne doivent pas être recalculés lors de la
    # confirmation de livraison. La sortie de stock est enregistrée une seule
    # fois lors de la facturation (bl_facturer).

    # Enregistre le livreur ET la date saisis dans le modal
    execute("""UPDATE bons_livraison
               SET statut='livre',
                   livreur=?,
                   date_livraison=COALESCE(?, date('now'))
               WHERE id=?""",
            (livreur, date_livraison, id))

    flash("BL " + bl['reference'] + " marqué comme livré.", "success")
    return redirect(url_for('bons_livraison_list'))


@app.route('/bons_livraison/<int:id>/facturer', methods=['POST'])
@login_required
def bl_facturer(id):
    bl = query("SELECT * FROM bons_livraison WHERE id=?", (id,), one=True)
    if not bl:
        flash("BL introuvable.", "danger")
        return redirect(url_for('bons_livraison_list'))
    if bl['statut'] != 'livre':
        flash("Seul un BL au statut 'Livré' peut être facturé.", "warning")
        return redirect(url_for('bons_livraison_list'))

    # Vérifier qu'une facture n'existe pas déjà pour ce BL
    exist = query("SELECT id FROM documents_vente WHERE bl_origine_id=? AND type_doc='facture'", (id,), one=True)
    if exist:
        flash("Une facture existe déjà pour ce BL.", "warning")
        return redirect(url_for('factures_list'))

    ref = next_ref('FAC')

    # ── Lignes BL avec totaux pré-calculés (copiés fidèlement depuis la commande) ──
    # On utilise total_ht/total_ttc stockés dans chaque ligne_bl et NON pas
    # prix_ht * quantite_livree : prix_ht peut être le prix COLIS alors que
    # quantite_livree est en UNITÉS — ce qui produirait un montant incorrect.
    lignes = query("SELECT * FROM lignes_bl WHERE bl_id=? ORDER BY num_ligne", (id,))

    total_ht  = 0.0
    total_tva = 0.0
    lignes_calc = []
    for l in lignes:
        qte_cmd = float(l['quantite_commandee'] or 0)
        qte_liv = float(l['quantite_livree']    or 0)
        qte_col = float((l['quantite_colis'] if 'quantite_colis' in l.keys() else 0) or 0)
        th_cmd  = float(l['total_ht']  or 0)
        ttc_cmd = float(l['total_ttc'] or 0)
        tva_p   = float(l['tva'] or 0)

        if qte_cmd > 0 and qte_liv != qte_cmd:
            # Livraison partielle : prorata fidèle aux montants originaux
            ratio   = qte_liv / qte_cmd
            th_liv  = round(th_cmd  * ratio, 2)
            ttc_liv = round(ttc_cmd * ratio, 2)
            qte_col = round(qte_col * ratio, 2)
        else:
            # Livraison complète : reprendre exactement les montants de la ligne
            th_liv  = round(th_cmd,  2)
            ttc_liv = round(ttc_cmd, 2)

        tva_liv = round(ttc_liv - th_liv, 2)
        total_ht  += th_liv
        total_tva += tva_liv
        remise_p = float(l['remise_pct'] if 'remise_pct' in l.keys() else 0)
        lignes_calc.append({
            'article_id':  l['article_id'],
            'designation': l['designation'],
            'qte'        : qte_liv,
            'qte_colis'  : qte_col,
            'prix_ht'    : float(l['prix_ht'] or 0),
            'remise_pct'  : remise_p,
            'tva'        : tva_p,
            'total_ht'   : th_liv,
            'total_ttc'  : ttc_liv,
            'num_ligne'  : l['num_ligne'],
        })

    total_ht  = round(total_ht,  2)
    total_tva = round(total_tva, 2)
    total_ttc = round(total_ht + total_tva, 2)

    # Récupérer la commande parente pour mode_paiement et date_echeance
    cmd_parent = None
    if bl['commande_id']:
        cmd_parent = query("SELECT * FROM documents_vente WHERE id=?", (bl['commande_id'],), one=True)

    mode_paiement = (cmd_parent['mode_paiement'] if cmd_parent else None) or bl.get('mode_paiement') or 'especes'
    date_echeance = (cmd_parent['date_echeance']  if cmd_parent else None)

    execute("""INSERT INTO documents_vente
               (type_doc,reference,client_id,depot_id,date_doc,date_echeance,statut,
                total_ht,total_tva,total_ttc,reste,
                mode_paiement,notes,bl_origine_id,doc_parent_id)
               VALUES('facture',?,?,?,date('now'),?,'en_attente',
                      ?,?,?,?,
                      ?,?,?,?)""",
            (ref, bl['client_id'], bl['depot_id'],
             date_echeance,
             total_ht, total_tva, total_ttc, total_ttc,
             mode_paiement, bl['notes'] or '',
             id, bl['commande_id']))

    fac_id = query("SELECT id FROM documents_vente WHERE reference=?", (ref,), one=True)['id']
    dep_id = resolve_depot_id(bl['depot_id'])

    for i, lc in enumerate(lignes_calc):
        execute("""INSERT INTO lignes_vente
                   (document_id,article_id,designation,quantite_unite,quantite_colis,
                    prix_ht,remise_pct,tva,total_ht,total_ttc,num_ligne)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (fac_id, lc['article_id'], lc['designation'],
                 lc['qte'], lc['qte_colis'], lc['prix_ht'], lc['remise_pct'], lc['tva'],
                 lc['total_ht'], lc['total_ttc'], i + 1))

        # ── Décrémenter le stock une seule fois ici ──
        if lc['article_id'] and lc['qte'] > 0:
            execute("""UPDATE stocks SET quantite_unite=MAX(0, quantite_unite-?)
                       WHERE article_id=? AND depot_id=?""",
                    (lc['qte'], lc['article_id'], dep_id))
            stock_now = query("SELECT quantite_unite FROM stocks WHERE article_id=? AND depot_id=?",
                              (lc['article_id'], dep_id), one=True)
            stock_apres = stock_now['quantite_unite'] if stock_now else 0
            execute("""INSERT INTO mouvements_stocks
                       (article_id,depot_id,type_mvt,quantite_unite,
                        doc_type,doc_id,doc_ref,stock_apres,operateur)
                       VALUES(?,?,'sortie',?,?,?,?,?,?)""",
                    (lc['article_id'], dep_id, lc['qte'],
                     'facture', fac_id, ref, stock_apres,
                     session.get('user_nom', '')))

    # Mettre à jour le BL → facturé
    execute("UPDATE bons_livraison SET statut='facture', facture_id=? WHERE id=?", (fac_id, id))
    # Mettre à jour la commande parente → facturée
    if bl['commande_id']:
        execute("UPDATE documents_vente SET statut='facturee' WHERE id=?", (bl['commande_id'],))

    # ── Règlement lié à la commande parente : imputer sur la facture générée ──
    rgl_lie = None
    if bl['commande_id']:
        rgl_lie = query(
            """SELECT * FROM reglements
               WHERE commande_id=? AND source_type='commande'
               ORDER BY date_creation DESC LIMIT 1""",
            (bl['commande_id'],), one=True)

    if rgl_lie:
        montant_rgl  = float(rgl_lie['montant'] or 0)
        nouveau_paye = min(montant_rgl, total_ttc)
        reste_fac    = round(max(0, total_ttc - nouveau_paye), 2)
        statut_fac   = 'reglee' if reste_fac <= 0 else 'partielle'
        execute("""UPDATE documents_vente
                   SET montant_paye=?, reste=?, statut=?
                   WHERE id=?""",
                (round(nouveau_paye, 2), reste_fac, statut_fac, fac_id))
        # Rattacher le règlement à la facture (source_type + source_id)
        execute("""UPDATE reglements
                   SET source_type='facture', source_id=?
                   WHERE id=?""",
                (fac_id, rgl_lie['id']))
        flash(f"Facture {ref} générée et marquée '{statut_fac}' (règlement {rgl_lie['reference']} imputé).", "success")
    else:
        flash(f"Facture {ref} générée depuis le BL {bl['reference']}.", "success")
    return redirect(url_for('factures_list'))


# ── Impression BL ─────────────────────────────────────────────────────
# ── Génération HTML : BON DE LIVRAISON (réutilisée par impression directe) ──
def _print_document_bl(id):
    cfg = get_cfg()
    bl = query("""SELECT bl.*, c.nom as client_nom, c.telephone as client_tel,
                         c.adresse as client_adresse, d.nom as depot_nom
                  FROM bons_livraison bl
                  LEFT JOIN clients c ON c.id=bl.client_id
                  LEFT JOIN depots d ON d.id=bl.depot_id
                  WHERE bl.id=?""", (id,), one=True)
    if not bl:
        return "BL introuvable", 404
    bl = dict(bl)
    lignes = [dict(r) for r in query("""SELECT lb.*,
                  COALESCE(lb.designation, a.designation) as designation,
                  COALESCE(a.colisage, 1) as colisage
                  FROM lignes_bl lb
                  LEFT JOIN articles a ON a.id=lb.article_id
                  WHERE lb.bl_id=? ORDER BY lb.num_ligne""", (id,))]
    devise = cfg.get('devise', 'FCFA')

    def fcfa(v):
        try: return f"{int(float(v or 0)):,}".replace(',', ' ')
        except: return '0'

    lignes_html = ''
    for l in lignes:
        qte_u   = int(float(l.get('quantite_livree') or l.get('quantite_commandee') or 0))
        col     = max(1, int(float(l.get('colisage') or 1)))
        qte_c   = qte_u // col
        pu      = float(l.get('prix_ht') or 0)
        th      = float(l.get('total_ht') or qte_u * pu)
        ttc     = float(l.get('total_ttc') or th)
        tva_pct = float(l.get('tva') or 0)
        tva_mnt = round(th * tva_pct / 100)
        qte_c_lbl = f"{qte_c} C" if col > 1 else '&#8212;'
        lignes_html += (
            '<tr>'
            f'<td>{l.get("designation") or "&#8212;"}</td>'
            f'<td style="text-align:center">{qte_u}</td>'
            f'<td style="text-align:center;color:#2563eb;font-weight:600">{qte_c_lbl}</td>'
            f'<td style="text-align:right">{fcfa(pu)} {devise}</td>'
            f'<td style="text-align:right">{fcfa(th)} {devise}</td>'
            f'<td style="text-align:right">{fcfa(tva_mnt)} {devise}</td>'
            f'<td style="text-align:right;font-weight:700">{fcfa(ttc)} {devise}</td>'
            '</tr>'
        )

    statut_map = {'brouillon':'Brouillon','livre':'Livré','facture':'Facturé','annule':'Annulé'}
    html = _print_page_html(
        titre='BON DE LIVRAISON',
        reference=bl['reference'],
        date_doc=bl.get('date_bl',''),
        statut=statut_map.get(bl.get('statut',''), bl.get('statut','')),
        statut_ok=bl.get('statut') in ('livre','facture'),
        tiers_label='Client', tiers_nom=bl.get('client_nom') or 'Client passager',
        tiers_tel=bl.get('client_tel',''), tiers_adresse=bl.get('client_adresse',''),
        nom_soc=cfg.get('nom_depot','Mon Commerce'),
        tel_soc=cfg.get('telephone',''),
        adr_soc=cfg.get('adresse',''),
        lignes_html=lignes_html,
        col_headers=['Désignation','Qté Livrée','Qté Colis','PU HT','HT','TVA','TTC'],
        ht_total=fcfa(bl.get('total_ht')), tva_total=fcfa(bl.get('total_tva')),
        ttc_total=fcfa(bl.get('total_ttc')), reste='0',
        devise=devise, doc_statut=bl.get('statut','')
    )
    return html

@app.route('/bons_livraison/<int:id>/imprimer')
@login_required
def bl_imprimer(id):
    html = _print_document_bl(id)
    if isinstance(html, tuple): return html
    return _document_pdf_response(html, 'bl', id, f"BL_{id}")


# ── Suppression BL ────────────────────────────────────────────────────
@app.route('/bons_livraison/<int:id>/supprimer', methods=['POST'])
@login_required
def bl_supprimer(id):
    bl = query("SELECT * FROM bons_livraison WHERE id=?", (id,), one=True)
    if not bl:
        flash("BL introuvable.", "danger")
        return redirect(url_for('bons_livraison_list'))
    # ── Blocage : BL déjà facturé ───────────────────────────────────
    if bl['statut'] == 'facture':
        flash(f"Le bon de livraison {bl['reference'] or ''} a déjà été facturé — "
              f"suppression impossible.", "danger")
        return redirect(url_for('bons_livraison_list'))
    if bl['commande_id']:
        execute("UPDATE documents_vente SET statut='en_attente' WHERE id=? AND statut='bl_cree'",
                (bl['commande_id'],))
    execute("DELETE FROM lignes_bl WHERE bl_id=?", (id,))
    execute("DELETE FROM bons_livraison WHERE id=?", (id,))
    flash("Bon de livraison " + (bl['reference'] or '') + " supprimé.", "success")
    return redirect(url_for('bons_livraison_list'))


# ── Avoirs clients ────────────────────────────────────────────────────
@app.route('/avoirs')
@login_required
def avoirs_list():
    cfg = get_cfg()
    avoirs = query("""
        SELECT av.*, c.nom as client_nom, dv.reference as facture_ref
        FROM avoirs_clients av
        LEFT JOIN clients c ON c.id=av.client_id
        LEFT JOIN documents_vente dv ON dv.id=av.facture_id
        ORDER BY av.date_creation DESC
    """)
    clients = query("""SELECT id,nom,prenom,code FROM clients WHERE actif=1
                       ORDER BY CASE WHEN code='CLI000' THEN 0 ELSE 1 END, nom""")
    factures = query("""SELECT id,reference,client_id,total_ttc FROM documents_vente
                        WHERE type_doc='facture' ORDER BY date_doc DESC""")
    client_defaut = query("SELECT * FROM clients WHERE code='CLI000' AND actif=1", one=True)
    return render_template('avoirs.html', cfg=cfg, avoirs=avoirs,
                           clients=clients, factures=factures,
                           client_defaut=client_defaut)




@app.route('/avoirs/<int:id>/imprimer')
@login_required
def avoir_imprimer(id):
    cfg = get_cfg()
    avoir = query("""
        SELECT av.*, c.nom as client_nom, c.telephone as client_tel, c.adresse as client_adresse
        FROM avoirs_clients av
        LEFT JOIN clients c ON c.id=av.client_id
        WHERE av.id=?
    """, (id,), one=True)
    if not avoir:
        return "Avoir introuvable", 404
    avoir = dict(avoir)  # sqlite3.Row → dict (évite TypeError sur .get(key, default))

    nom_soc = cfg.get('nom_depot', 'Mon Commerce')
    tel_soc = cfg.get('telephone', '')
    adr_soc = cfg.get('adresse', '')
    devise  = cfg.get('devise', 'FCFA')

    def fcfa(v):
        try: return f"{int(float(v or 0)):,}".replace(',', ' ')
        except: return '0'

    types_avoir = {
        'retour': 'Retour marchandise',
        'remise': 'Remise commerciale',
        'erreur': 'Erreur facturation',
        'litige': 'Litige',
    }
    modes_remb = {
        'credit':  'Crédit sur compte',
        'especes': 'Espèces',
        'virement': 'Virement',
    }
    type_lbl = types_avoir.get(avoir['type_avoir'] or '', avoir['type_avoir'] or '—')
    mode_lbl = modes_remb.get(avoir['mode_remboursement'] or '', avoir['mode_remboursement'] or '—')
    statut_lbl = 'Validé' if avoir['statut'] != 'en_attente' else 'En attente'

    # Ligne unique résumant l'avoir (pas de lignes détaillées sur les avoirs simples)
    lignes_html = (
        '<tr>'
        f'<td>{type_lbl}</td>'
        f'<td colspan="2" style="color:#64748b;font-size:11px;">{avoir.get("motif") or "—"}</td>'
        f'<td style="text-align:right">—</td>'
        f'<td style="text-align:right">—</td>'
        f'<td style="text-align:right">{fcfa(avoir["total_ht"])} {devise}</td>'
        f'<td style="text-align:right">{fcfa(avoir["total_tva"])} {devise}</td>'
        f'<td style="text-align:right;font-weight:700">{fcfa(avoir["total_ttc"])} {devise}</td>'
        '</tr>'
        f'<tr><td colspan="8" style="font-size:11px;color:#64748b;padding-top:4px;">'
        f'Mode de remboursement : <strong>{mode_lbl}</strong></td></tr>'
    )

    html = _print_page_html(
        titre='AVOIR CLIENT',
        reference=avoir['reference'],
        date_doc=avoir.get('date_avoir', ''),
        statut=statut_lbl,
        statut_ok=(avoir['statut'] != 'en_attente'),
        tiers_label='Client',
        tiers_nom=avoir.get('client_nom') or 'Client passager',
        tiers_tel=avoir.get('client_tel', ''),
        tiers_adresse=avoir.get('client_adresse', ''),
        nom_soc=nom_soc, tel_soc=tel_soc, adr_soc=adr_soc,
        lignes_html=lignes_html,
        col_headers=['Type avoir', 'Motif', '', 'PU HT', 'Rem.', 'HT', 'TVA', 'TTC'],
        ht_total=fcfa(avoir['total_ht']),
        tva_total=fcfa(avoir['total_tva']),
        ttc_total=fcfa(avoir['total_ttc']),
        reste='0',
        devise=devise,
        doc_statut=avoir['statut'],
    )
    return _document_pdf_response(html, 'avoir', id, f"Avoir_{id}")



@app.route('/avoirs/<int:id>/toggle_statut', methods=['POST'])
@login_required
def avoir_toggle_statut(id):
    avoir = query("SELECT statut FROM avoirs_clients WHERE id=?", (id,), one=True)
    if not avoir:
        flash("Avoir introuvable.", "danger")
        return redirect(url_for('avoirs_list'))
    nouveau = 'valide' if avoir['statut'] == 'en_attente' else 'en_attente'
    execute("UPDATE avoirs_clients SET statut=? WHERE id=?", (nouveau, id))
    return redirect(url_for('avoirs_list'))

@app.route('/avoirs/add', methods=['POST'])
@login_required
def avoir_add():
    f = request.form
    n = query("SELECT COUNT(*) as c FROM avoirs_clients", one=True)['c']
    ref = "AV{:05d}".format(n + 1)
    ht = float(f.get('total_ht',0) or 0)
    tva_r = float(get_cfg().get('tva_defaut', 0) or 0) / 100
    tva = round(ht * tva_r)
    ttc = ht + tva
    date_avoir  = f.get('date_avoir', date.today().isoformat())
    facture_id  = f.get('facture_id') or None
    client_id   = f['client_id']
    motif       = f.get('motif', '')
    type_avoir  = f.get('type_avoir', 'retour')

    avoir_id = execute("""INSERT INTO avoirs_clients
               (reference,facture_id,client_id,date_avoir,motif,type_avoir,
                total_ht,total_tva,total_ttc,mode_remboursement,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (ref, facture_id, client_id, date_avoir,
             motif, type_avoir,
             round(ht,2), round(tva,2), round(ttc,2),
             f.get('mode_remboursement','credit'), f.get('notes','')))

    # ── Mise à jour de la facture client liée ─────────────────────────
    if facture_id:
        facture = query("SELECT * FROM documents_vente WHERE id=?", (facture_id,), one=True)
        if facture:
            nouveau_reste   = round(max(0, (facture['reste'] or 0) - ttc), 2)
            nouveau_paye    = round((facture['montant_paye'] or 0) + ttc, 2)
            nouveau_statut  = 'reglee' if nouveau_reste <= 0 else 'partielle'
            execute("""UPDATE documents_vente
                       SET reste=?, montant_paye=?, statut=? WHERE id=?""",
                    (nouveau_reste, nouveau_paye, nouveau_statut, facture_id))

    flash("Avoir " + ref + " créé. Cliquez sur « Comptabiliser » pour l'importer en comptabilité.", "success")
    return redirect(url_for('avoirs_list'))


@app.route('/avoirs/delete/<int:id>')
@login_required
def avoir_delete(id):
    av = query("SELECT * FROM avoirs_clients WHERE id=?", (id,), one=True)
    if av:
        ttc = float(av['total_ttc'] or 0)
        # ── 1. Supprimer l'écriture comptable liée ──
        execute("DELETE FROM ecritures_comptables WHERE source='avoir_client' AND source_id=?", (id,))
        # ── 2. Reverser le montant sur la facture client liée ──
        if av['facture_id']:
            facture = query("SELECT * FROM documents_vente WHERE id=?", (av['facture_id'],), one=True)
            if facture:
                nouveau_paye   = round(max(0, (facture['montant_paye'] or 0) - ttc), 2)
                nouveau_reste  = round(max(0, (facture['total_ttc'] or 0) - nouveau_paye), 2)
                nouveau_statut = 'reglee' if nouveau_reste <= 0 else ('partielle' if nouveau_paye > 0 else 'en_attente')
                execute("""UPDATE documents_vente
                           SET montant_paye=?, reste=?, statut=? WHERE id=?""",
                        (nouveau_paye, nouveau_reste, nouveau_statut, av['facture_id']))
    execute("DELETE FROM avoirs_clients WHERE id=?", (id,))
    flash("Avoir supprimé. Recettes mises à jour.", "success")
    return redirect(url_for('avoirs_list'))


# ══════════════════════════════════════════════════════════════════════
#  CYCLE D'ACHAT COMPLET
# ══════════════════════════════════════════════════════════════════════

# ── Convertir devis achat → commande ─────────────────────────────────
@app.route('/achats/<int:id>/convertir_commande', methods=['POST'])
@login_required
def achat_devis_convertir(id):
    devis = query("SELECT * FROM documents_achat WHERE id=? AND type_doc='devis'", (id,), one=True)
    if not devis:
        flash("Devis achat introuvable.", "danger")
        return redirect(url_for('achats_list'))
    n = query("SELECT COUNT(*) as c FROM documents_achat WHERE type_doc='commande'", one=True)['c']
    ref = "CA{:05d}".format(n + 1)
    execute("""INSERT INTO documents_achat
               (type_doc,reference,fournisseur_id,depot_id,date_doc,date_livraison_prevue,
                statut,total_ht,total_tva,total_ttc,notes)
               VALUES('commande',?,?,?,date('now'),?,'en_attente',?,?,?,?)""",
            (ref, devis['fournisseur_id'], devis['depot_id'],
             devis['date_livraison_prevue'],
             devis['total_ht'], devis['total_tva'], devis['total_ttc'],
             devis['notes']))
    new_id = query("SELECT id FROM documents_achat WHERE reference=?", (ref,), one=True)['id']
    for l in query("SELECT * FROM lignes_achat WHERE document_id=?", (id,)):
        execute("""INSERT INTO lignes_achat
                   (document_id,article_id,designation,quantite_unite,prix_achat_ht,tva,total_ht,total_ttc)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (new_id, l['article_id'], l['designation'],
                 l['quantite_unite'], l['prix_achat_ht'], l['tva'],
                 l['total_ht'], l['total_ttc']))
    execute("UPDATE documents_achat SET statut='converti' WHERE id=?", (id,))
    flash("Devis achat converti en commande " + ref + ".", "success")
    return redirect(url_for('achats_list'))





# ── Avoirs fournisseurs ───────────────────────────────────────────────
@app.route('/avoirs_fournisseurs')
@login_required
def avoirs_fournisseurs_list():
    cfg = get_cfg()
    avoirs = query("""SELECT av.*, f.nom as fourn_nom, ff.reference as facture_ref
                      FROM avoirs_fournisseurs av
                      LEFT JOIN fournisseurs f ON f.id=av.fournisseur_id
                      LEFT JOIN factures_fournisseurs ff ON ff.id=av.facture_fourn_id
                      ORDER BY av.date_creation DESC""")
    fournisseurs = query("SELECT id,nom FROM fournisseurs WHERE actif=1 ORDER BY nom")
    factures_fourn = query("""SELECT 'ff'||CAST(id AS TEXT) as uid, id, 'ff' as source,
                                     reference, fournisseur_id,
                                     total_ht, total_ttc, reste
                              FROM factures_fournisseurs
                              ORDER BY date_facture DESC""")
    commandes_achat = query("""SELECT 'da'||CAST(id AS TEXT) as uid, id, 'da' as source,
                                      reference, fournisseur_id,
                                      total_ht, total_ttc, reste
                               FROM documents_achat
                               WHERE statut NOT IN ('annule')
                               ORDER BY date_doc DESC""")
    return render_template('avoirs_fournisseurs.html', cfg=cfg, avoirs=avoirs,
                           fournisseurs=fournisseurs, factures_fourn=factures_fourn,
                           commandes_achat=commandes_achat)


@app.route('/avoirs_fournisseurs/add', methods=['POST'])
@login_required
def avoir_fourn_add():
    f = request.form
    n = query("SELECT COUNT(*) as c FROM avoirs_fournisseurs", one=True)['c']
    ref = "AVF{:05d}".format(n + 1)
    ht  = float(f.get('total_ht',0) or 0)
    tva_r = float(get_cfg().get('tva_defaut', 0) or 0) / 100
    tva = round(ht * tva_r)
    ttc = ht + tva
    date_avoir = f.get('date_avoir', date.today().isoformat())
    # Valeur format "ff:ID" (facture) ou "da:ID" (commande achat) ou vide
    raw_doc = f.get('facture_fourn_id') or ''
    facture_fourn_id  = None
    commande_achat_id = None
    if raw_doc.startswith('ff:'):
        facture_fourn_id = int(raw_doc[3:])
    elif raw_doc.startswith('da:'):
        commande_achat_id = int(raw_doc[3:])
    fournisseur_id   = f['fournisseur_id']
    motif            = f.get('motif', '')
    type_avoir       = f.get('type_avoir', 'retour')

    avoir_id = execute("""INSERT INTO avoirs_fournisseurs
               (reference,facture_fourn_id,fournisseur_id,date_avoir,motif,type_avoir,
                total_ht,total_tva,total_ttc,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (ref, facture_fourn_id, fournisseur_id, date_avoir,
             motif, type_avoir,
             round(ht,2), round(tva,2), round(ttc,2), f.get('notes','')))

    # ── Mise à jour du document lié (facture fournisseur ou commande achat) ──
    if facture_fourn_id:
        ff = query("SELECT * FROM factures_fournisseurs WHERE id=?", (facture_fourn_id,), one=True)
        if ff:
            nouveau_paye  = round((ff['montant_paye'] or 0) + ttc, 2)
            nouveau_reste = round(max(0, (ff['total_ttc'] or 0) - nouveau_paye), 2)
            nouveau_statut = 'reglee' if nouveau_reste <= 0 else 'partielle'
            execute("""UPDATE factures_fournisseurs
                       SET montant_paye=?, reste=?, statut=? WHERE id=?""",
                    (nouveau_paye, nouveau_reste, nouveau_statut, facture_fourn_id))
    elif commande_achat_id:
        da = query("SELECT * FROM documents_achat WHERE id=?", (commande_achat_id,), one=True)
        if da:
            nouveau_paye  = round((da['montant_paye'] or 0) + ttc, 2)
            nouveau_reste = round(max(0, (da['total_ttc'] or 0) - nouveau_paye), 2)
            execute("""UPDATE documents_achat
                       SET montant_paye=?, reste=? WHERE id=?""",
                    (nouveau_paye, nouveau_reste, commande_achat_id))

    flash("Avoir fournisseur " + ref + " créé. Cliquez sur « Comptabiliser » pour l'importer en comptabilité.", "success")
    return redirect(url_for('avoirs_fournisseurs_list'))


@app.route('/avoirs_fournisseurs/delete/<int:id>')
@login_required
def avoir_fourn_delete(id):
    av = query("SELECT * FROM avoirs_fournisseurs WHERE id=?", (id,), one=True)
    if av:
        ttc = float(av['total_ttc'] or 0)
        # ── 1. Supprimer l'écriture comptable liée ──
        execute("DELETE FROM ecritures_comptables WHERE source='avoir_fourn' AND source_id=?", (id,))
        # ── 2. Reverser le montant sur la facture fournisseur liée ──
        if av['facture_fourn_id']:
            ff = query("SELECT * FROM factures_fournisseurs WHERE id=?", (av['facture_fourn_id'],), one=True)
            if ff:
                nouveau_paye  = round(max(0, (ff['montant_paye'] or 0) - ttc), 2)
                nouveau_reste = round(max(0, (ff['total_ttc'] or 0) - nouveau_paye), 2)
                nouveau_statut = 'reglee' if nouveau_reste <= 0 else ('partielle' if nouveau_paye > 0 else 'en_attente')
                execute("""UPDATE factures_fournisseurs
                           SET montant_paye=?, reste=?, statut=? WHERE id=?""",
                        (nouveau_paye, nouveau_reste, nouveau_statut, av['facture_fourn_id']))
    execute("DELETE FROM avoirs_fournisseurs WHERE id=?", (id,))
    flash("Avoir supprimé. Dépenses mises à jour.", "success")
    return redirect(url_for('avoirs_fournisseurs_list'))



@app.route('/stock/inventaire', methods=['POST'])
@login_required
def stock_inventaire():
    """Validation d'un inventaire physique :
       - Reçoit un JSON `lignes_json` avec [{art, dep, sys, phys, diff, val}, …]
       - Met à jour quantite_unite par dépôt
       - Génère un mouvement d'ajustement (entree/sortie) pour chaque écart non nul
    """
    import json as _json
    raw = request.form.get('lignes_json', '')
    try:
        lignes = _json.loads(raw) if raw else []
    except Exception:
        lignes = []

    # Fallback : ancien format (champs qte_<id>) pour compat
    if not lignes:
        for key, val in request.form.items():
            if key.startswith('qte_'):
                try:
                    sid = int(key.replace('qte_',''))
                    qte = float(val or 0)
                    execute("UPDATE stocks SET quantite_unite=? WHERE id=?", (qte, sid))
                except (ValueError, TypeError):
                    pass
        flash("Inventaire enregistré.", "success")
        return redirect(url_for('inventaire_list'))

    nb_ok = nb_pos = nb_neg = 0
    op = session.get('user') or 'INVENTAIRE'
    today = date.today().isoformat()

    for l in lignes:
        try:
            art_id = int(l.get('art'))
            dep_id = int(l.get('dep'))
            sys_q  = float(l.get('sys') or 0)
            phys_q = float(l.get('phys') or 0)
            diff   = phys_q - sys_q
        except (ValueError, TypeError):
            continue

        # Mise à jour stock — la quantité physique devient la nouvelle référence
        execute("""INSERT OR IGNORE INTO stocks(article_id, depot_id, quantite_unite)
                   VALUES(?,?,0)""", (art_id, dep_id))
        execute("UPDATE stocks SET quantite_unite=? WHERE article_id=? AND depot_id=?",
                (phys_q, art_id, dep_id))

        if diff == 0:
            nb_ok += 1
            continue

        # Mouvement d'ajustement
        type_mvt = 'entree' if diff > 0 else 'sortie'
        if diff > 0: nb_pos += 1
        else:        nb_neg += 1

        execute("""INSERT INTO mouvements_stocks
                   (article_id, depot_id, type_mvt, quantite_unite,
                    doc_type, doc_ref, stock_apres, operateur, date_mvt, notes)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (art_id, dep_id, type_mvt, abs(diff),
                 'inventaire', f'INV-{today}', phys_q, op, today,
                 f"Ajustement inventaire ({'+' if diff>0 else ''}{int(diff)} unités)"))

    flash(f"Inventaire validé : {nb_ok} conformes, {nb_pos} excédent(s), {nb_neg} manquant(s).", "success")
    return redirect(url_for('inventaire_list'))


@app.route('/stock/transfert', methods=['POST'])
@login_required
def stock_transfert():
    """Transfert de stock entre dépôts"""
    f = request.form
    article_id = f.get('article_id')
    depot_src   = f.get('depot_src')
    depot_dst   = f.get('depot_dst')
    qte         = float(f.get('quantite', 0) or 0)
    if not (article_id and depot_src and depot_dst and qte > 0):
        flash("Données invalides.", "danger")
        return redirect(url_for('stock_list'))
    if depot_src == depot_dst:
        flash("Dépôt source et destination identiques.", "warning")
        return redirect(url_for('stock_list'))
    execute("UPDATE stocks SET quantite_unite=MAX(0,quantite_unite-?) WHERE article_id=? AND depot_id=?",
            (qte, article_id, depot_src))
    execute("INSERT OR IGNORE INTO stocks(article_id,depot_id,quantite_unite) VALUES(?,?,0)",
            (article_id, depot_dst))
    execute("UPDATE stocks SET quantite_unite=quantite_unite+? WHERE article_id=? AND depot_id=?",
            (qte, article_id, depot_dst))
    flash(f"Transfert de {qte:.0f} unités effectué.", "success")
    return redirect(url_for('stock_list'))


# ══════════════════════════════════════════════════════════════════════
#  MODULE ATELIER — ÉQUIPEMENTS & TICKETS
# ══════════════════════════════════════════════════════════════════════

STATUTS_TICKET = {
    'recu':        ('📥', '#3b82f6', 'Reçu'),
    'diagnostic':  ('🔍', '#f59e0b', 'Diagnostic'),
    'en_cours':    ('🔧', '#8b5cf6', 'En cours'),
    'attente_pièces': ('⏳', '#f97316', 'Attente pièces'),
    'pret':        ('✅', '#16a34a', 'Prêt'),
    'livre':       ('📦', '#0ea5e9', 'Livré'),
    'annule':      ('❌', '#dc2626', 'Annulé'),
}
PRIORITES = {
    'basse':    ('#94a3b8', '🟢'),
    'normale':  ('#2563eb', '🔵'),
    'haute':    ('#f59e0b', '🟠'),
    'urgente':  ('#dc2626', '🔴'),
}

def next_ref_ticket():
    return _next_ref_seq('tickets', 'TKT')

def _atelier_actif():
    """Retourne True si le module Atelier est activé dans les paramètres."""
    cfg = get_cfg()
    return cfg.get('module_atelier') == 'oui'

def _require_atelier(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _atelier_actif():
            flash("⚙️ Le module Atelier est désactivé. Activez-le dans les Paramètres.", "warning")
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


# ── ÉQUIPEMENTS ────────────────────────────────────────────────────────

@app.route('/equipements')
@login_required
@_require_atelier
def equipements_list():
    cfg    = get_cfg()
    q      = request.args.get('q', '').strip()
    client_id = request.args.get('client_id', '').strip()
    if client_id:
        equipements = query("""
            SELECT e.*, c.nom||' '||COALESCE(c.prenom,'') as client_nom, c.telephone as client_tel
            FROM equipements e
            LEFT JOIN clients c ON c.id=e.client_id
            WHERE e.client_id=?
            ORDER BY e.id DESC
        """, (client_id,))
        client = query("SELECT * FROM clients WHERE id=?", (client_id,), one=True)
    elif q:
        like = f'%{q}%'
        equipements = query("""
            SELECT e.*, c.nom||' '||COALESCE(c.prenom,'') as client_nom, c.telephone as client_tel
            FROM equipements e
            LEFT JOIN clients c ON c.id=e.client_id
            WHERE c.nom LIKE ? OR c.prenom LIKE ? OR e.marque LIKE ? OR e.modele LIKE ?
               OR e.numero_serie LIKE ?
            ORDER BY e.id DESC
        """, (like, like, like, like, like))
        client = None
    else:
        equipements = query("""
            SELECT e.*, c.nom||' '||COALESCE(c.prenom,'') as client_nom, c.telephone as client_tel
            FROM equipements e
            LEFT JOIN clients c ON c.id=e.client_id
            ORDER BY e.id DESC
        """)
        client = None
    clients_all = query("""
        SELECT id, nom||' '||COALESCE(prenom,'') as label, telephone
        FROM clients WHERE actif=1 ORDER BY nom
    """)
    return render_template('equipements.html',
        equipements=equipements,
        clients_all=clients_all,
        client=client,
        client_id=client_id,
        cfg=cfg
    )


@app.route('/equipements/add', methods=['POST'])
@login_required
@_require_atelier
def equipement_add():
    f = request.form
    client_id = _safe_fk(f.get('client_id'))
    if not client_id:
        flash("Client requis.", "danger")
        return redirect(request.form.get('redirect_url') or url_for('equipements_list'))
    execute("""INSERT INTO equipements
               (client_id, type_appareil, marque, modele, numero_serie, couleur, description)
               VALUES(?,?,?,?,?,?,?)""",
            (client_id,
             f.get('type_appareil', 'Autre'),
             f.get('marque', '').strip(),
             f.get('modele', '').strip(),
             f.get('numero_serie', '').strip() or None,
             f.get('couleur', '').strip() or None,
             f.get('description', '').strip() or None))
    flash("✅ Équipement ajouté.", "success")
    return redirect(request.form.get('redirect_url') or url_for('equipements_list'))


@app.route('/equipements/edit/<int:id>', methods=['POST'])
@login_required
@_require_atelier
def equipement_edit(id):
    f = request.form
    client_id = _safe_fk(f.get('client_id'))
    execute("""UPDATE equipements SET
               client_id=?, type_appareil=?, marque=?, modele=?,
               numero_serie=?, couleur=?, description=?
               WHERE id=?""",
            (client_id,
             f.get('type_appareil', 'Autre'),
             f.get('marque', '').strip(),
             f.get('modele', '').strip(),
             f.get('numero_serie', '').strip() or None,
             f.get('couleur', '').strip() or None,
             f.get('description', '').strip() or None,
             id))
    flash("✅ Équipement modifié.", "success")
    return redirect(url_for('equipements_list'))


@app.route('/equipements/delete/<int:id>')
@login_required
@_require_atelier
def equipement_delete(id):
    execute("DELETE FROM equipements WHERE id=?", (id,))
    flash("🗑 Équipement supprimé.", "success")
    return redirect(url_for('equipements_list'))


@app.route('/api/equipements/<int:client_id>')
@login_required
def api_equipements(client_id):
    rows = query("""
        SELECT id,
               type_appareil||' '||COALESCE(marque,'')||' '||COALESCE(modele,'') as label
        FROM equipements WHERE client_id=? ORDER BY id DESC
    """, (client_id,))
    return jsonify([dict(r) for r in rows])


@app.route('/api/equipement/add', methods=['POST'])
@login_required
def api_equipement_add():
    """Création rapide d'équipement depuis une modale ticket, renvoie JSON avec id et label."""
    f = request.form
    client_id = _safe_fk(f.get('client_id'))
    if not client_id:
        return jsonify({'error': 'Client requis.'}), 400
    execute("""INSERT INTO equipements
               (client_id, type_appareil, marque, modele, numero_serie, couleur, description)
               VALUES(?,?,?,?,?,?,?)""",
            (client_id,
             f.get('type_appareil', 'Autre'),
             f.get('marque', '').strip(),
             f.get('modele', '').strip(),
             f.get('numero_serie', '').strip() or None,
             f.get('couleur', '').strip() or None,
             f.get('description', '').strip() or None))
    row = query("""SELECT id,
                   type_appareil||' '||COALESCE(marque,'')||' '||COALESCE(modele,'') as label
                   FROM equipements WHERE client_id=? ORDER BY id DESC LIMIT 1""",
                (client_id,), one=True)
    return jsonify({'id': row['id'], 'label': row['label']})


# ── TICKETS ────────────────────────────────────────────────────────────

@app.route('/tickets')
@login_required
@_require_atelier
def tickets_list():
    cfg = get_cfg()
    statut_filtre = request.args.get('statut', '').strip()
    if statut_filtre:
        tickets = query("""
            SELECT t.*,
                   c.nom||' '||COALESCE(c.prenom,'') as client_nom,
                   c.telephone as client_tel,
                   e.type_appareil, e.marque||' '||COALESCE(e.modele,'') as appareil_label,
                   emp.nom as technicien_nom
            FROM tickets t
            LEFT JOIN clients  c   ON c.id=t.client_id
            LEFT JOIN equipements e ON e.id=t.equipement_id
            LEFT JOIN employes emp  ON emp.id=t.technicien_id
            WHERE t.statut=?
            ORDER BY t.date_creation DESC
        """, (statut_filtre,))
    else:
        tickets = query("""
            SELECT t.*,
                   c.nom||' '||COALESCE(c.prenom,'') as client_nom,
                   c.telephone as client_tel,
                   e.type_appareil, e.marque||' '||COALESCE(e.modele,'') as appareil_label,
                   emp.nom as technicien_nom
            FROM tickets t
            LEFT JOIN clients  c   ON c.id=t.client_id
            LEFT JOIN equipements e ON e.id=t.equipement_id
            LEFT JOIN employes emp  ON emp.id=t.technicien_id
            ORDER BY t.date_creation DESC
        """)
    clients_all = query("""
        SELECT id, nom||' '||COALESCE(prenom,'') as label, telephone
        FROM clients WHERE actif=1 ORDER BY nom
    """)
    techniciens = query("SELECT id, nom, prenom FROM employes WHERE actif=1 ORDER BY nom")
    today = date.today().isoformat()
    return render_template('tickets.html',
        tickets=tickets,
        clients_all=clients_all,
        techniciens=techniciens,
        statut_filtre=statut_filtre,
        STATUTS_TICKET=STATUTS_TICKET,
        PRIORITES=PRIORITES,
        today=today,
        cfg=cfg
    )


@app.route('/tickets/add', methods=['POST'])
@login_required
@_require_atelier
def ticket_add():
    f = request.form
    client_id = _safe_fk(f.get('client_id'))
    if not client_id:
        flash("Client requis.", "danger")
        return redirect(url_for('tickets_list'))
    ref = next_ref_ticket()
    execute("""INSERT INTO tickets
               (reference, client_id, equipement_id, type_panne, description_panne,
                accessoires, mot_de_passe, statut, priorite, technicien_id,
                date_reception, date_prevue, cout_estime, notes)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ref,
             client_id,
             _safe_fk(f.get('equipement_id')),
             f.get('type_panne', '').strip() or None,
             f.get('description_panne', '').strip() or None,
             f.get('accessoires', '').strip() or None,
             f.get('mot_de_passe', '').strip() or None,
             f.get('statut', 'recu'),
             f.get('priorite', 'normale'),
             _safe_fk(f.get('technicien_id')),
             f.get('date_reception') or date.today().isoformat(),
             f.get('date_prevue') or None,
             float(f.get('cout_estime') or 0),
             f.get('notes', '').strip() or None))
    flash(f"✅ Ticket {ref} créé.", "success")
    return redirect(url_for('tickets_list'))


@app.route('/tickets/edit/<int:id>', methods=['POST'])
@login_required
@_require_atelier
def ticket_edit(id):
    f = request.form
    client_id = _safe_fk(f.get('client_id'))
    execute("""UPDATE tickets SET
               client_id=?, equipement_id=?, type_panne=?, description_panne=?,
               accessoires=?, mot_de_passe=?, statut=?, priorite=?,
               technicien_id=?, date_reception=?, date_prevue=?,
               cout_estime=?, cout_final=?, diagnostic=?, travaux_effectues=?, notes=?
               WHERE id=?""",
            (client_id,
             _safe_fk(f.get('equipement_id')),
             f.get('type_panne', '').strip() or None,
             f.get('description_panne', '').strip() or None,
             f.get('accessoires', '').strip() or None,
             f.get('mot_de_passe', '').strip() or None,
             f.get('statut', 'recu'),
             f.get('priorite', 'normale'),
             _safe_fk(f.get('technicien_id')),
             f.get('date_reception') or date.today().isoformat(),
             f.get('date_prevue') or None,
             float(f.get('cout_estime') or 0),
             float(f.get('cout_final') or 0),
             f.get('diagnostic', '').strip() or None,
             f.get('travaux_effectues', '').strip() or None,
             f.get('notes', '').strip() or None,
             id))
    # Si statut livré → enregistrer date clôture
    statut = f.get('statut', '')
    if statut in ('livre', 'annule'):
        execute("UPDATE tickets SET date_cloture=? WHERE id=? AND date_cloture IS NULL",
                (date.today().isoformat(), id))
    flash("✅ Ticket modifié.", "success")
    return redirect(url_for('tickets_list'))


@app.route('/tickets/statut/<int:id>/<statut>')
@login_required
@_require_atelier
def ticket_statut(id, statut):
    if statut not in STATUTS_TICKET:
        flash("Statut invalide.", "danger")
        return redirect(url_for('tickets_list'))
    execute("UPDATE tickets SET statut=? WHERE id=?", (statut, id))
    if statut in ('livre', 'annule'):
        execute("UPDATE tickets SET date_cloture=? WHERE id=? AND date_cloture IS NULL",
                (date.today().isoformat(), id))
    flash(f"Statut mis à jour : {STATUTS_TICKET[statut][2]}", "success")
    return redirect(url_for('tickets_list'))


@app.route('/tickets/delete/<int:id>')
@login_required
@_require_atelier
def ticket_delete(id):
    execute("DELETE FROM tickets WHERE id=?", (id,))
    flash("🗑 Ticket supprimé.", "success")
    return redirect(url_for('tickets_list'))


@app.route('/tickets/regler', methods=['POST'])
@login_required
@_require_atelier
def ticket_regler():
    f = request.form
    ticket_id = int(f.get('ticket_id', 0))
    montant    = float(f.get('montant', 0) or 0)
    if not ticket_id or montant <= 0:
        flash("Données invalides.", "danger")
        return redirect(url_for('tickets_list'))
    client_id  = _safe_fk(f.get('client_id'))
    mode       = f.get('mode_paiement', 'especes')
    date_rgl   = f.get('date_reglement') or date.today().isoformat()
    notes      = f.get('notes', '').strip() or None
    execute("""INSERT INTO reglements_tickets
               (ticket_id, client_id, montant, mode_paiement, date_reglement, notes)
               VALUES(?,?,?,?,?,?)""",
            (ticket_id, client_id, montant, mode, date_rgl, notes))
    # Recalculer montant_regle
    total = query("SELECT COALESCE(SUM(montant),0) as s FROM reglements_tickets WHERE ticket_id=?",
                  (ticket_id,), one=True)['s']
    execute("UPDATE tickets SET montant_regle=? WHERE id=?", (total, ticket_id))
    # Comptabilité : écriture recette
    ticket = query("SELECT * FROM tickets WHERE id=?", (ticket_id,), one=True)
    if ticket:
        execute("""INSERT OR IGNORE INTO ecritures_comptables
                   (type_ecriture, date_ecriture, categorie, libelle, montant,
                    mode_paiement, source, source_id)
                   VALUES('recette',?,?,?,?,?,?,?)""",
                (date_rgl, 'Atelier / Réparation',
                 f"Règlement ticket {ticket['reference']}",
                 montant, mode, 'ticket', ticket_id))
    flash(f"✅ Règlement de {montant:,.0f} enregistré.", "success")
    return redirect(url_for('tickets_list'))


@app.route('/tickets/pdf/<int:id>')
@login_required
@_require_atelier
def ticket_pdf(id):
    """Fiche ticket imprimable (HTML → impression navigateur)."""
    ticket = query("""
        SELECT t.*,
               c.nom||' '||COALESCE(c.prenom,'') as client_nom,
               c.telephone as client_tel, c.adresse as client_adresse,
               e.type_appareil, e.marque, e.modele, e.numero_serie, e.couleur,
               emp.nom as technicien_nom
        FROM tickets t
        LEFT JOIN clients  c   ON c.id=t.client_id
        LEFT JOIN equipements e ON e.id=t.equipement_id
        LEFT JOIN employes emp  ON emp.id=t.technicien_id
        WHERE t.id=?
    """, (id,), one=True)
    if not ticket:
        flash("Ticket introuvable.", "danger")
        return redirect(url_for('tickets_list'))
    cfg = get_cfg()
    reglements = query("""
        SELECT * FROM reglements_tickets WHERE ticket_id=? ORDER BY date_reglement
    """, (id,))
    html = _ticket_pdf_html(ticket, cfg, reglements)
    html = _inject_print_format(html, _print_format_css(_current_print_format(), document=True))
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


def _ticket_pdf_html(t, cfg, reglements):
    """Génère le HTML de la fiche ticket imprimable."""
    nom_depot = cfg.get('nom_depot', 'MON ATELIER')
    devise    = cfg.get('devise', 'FCFA')
    adresse   = cfg.get('adresse', '')
    tel       = cfg.get('telephone', '')
    st = STATUTS_TICKET.get(t['statut'], ('📥', '#3b82f6', 'Reçu'))
    pr = PRIORITES.get(t['priorite'], ('#2563eb', '●'))
    cout = t['cout_final'] or t['cout_estime'] or 0
    regle = t['montant_regle'] or 0
    reste = max(0, cout - regle)
    rgl_rows = ''
    for r in reglements:
        rgl_rows += f"<tr><td>{r['date_reglement']}</td><td>{r['mode_paiement']}</td><td style='text-align:right;font-weight:700'>{r['montant']:,.0f} {devise}</td></tr>"
    if not rgl_rows:
        rgl_rows = "<tr><td colspan='3' style='color:#94a3b8;text-align:center;'>Aucun règlement</td></tr>"
    appareil_info = ' '.join(filter(None, [
        t['type_appareil'], t['marque'], t['modele'],
        f"SN:{t['numero_serie']}" if t['numero_serie'] else None,
        t['couleur']
    ]))
    return f"""<!doctype html><html lang="fr"><head>
<meta charset="utf-8"><title>Ticket {t['reference']} — {nom_depot}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;font-size:12px;background:#fff;color:#1e293b;padding:20px}}
  .header{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px;padding-bottom:12px;border-bottom:2px solid #1a3a6c}}
  .shop{{}}
  .shop-name{{font-size:18px;font-weight:800;color:#1a3a6c}}
  .shop-sub{{font-size:11px;color:#64748b;margin-top:2px}}
  .ref-block{{text-align:right}}
  .ref{{font-size:22px;font-weight:800;color:#2563eb}}
  .badge{{display:inline-block;padding:3px 10px;border-radius:20px;font-size:10px;font-weight:700;background:{st[1]}22;color:{st[1]};border:1.5px solid {st[1]}55;margin-top:4px}}
  .two{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}}
  .card{{border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px}}
  .card-title{{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin-bottom:6px}}
  .row{{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #f1f5f9}}
  .row:last-child{{border:none}}
  .rlabel{{color:#64748b}}
  .rval{{font-weight:600}}
  table{{width:100%;border-collapse:collapse;margin-top:8px}}
  th{{background:#1a3a6c;color:white;padding:6px 8px;font-size:10px;text-align:left}}
  td{{padding:5px 8px;border-bottom:1px solid #f1f5f9}}
  .total-row{{font-weight:700;background:#f8fafc}}
  .footer{{margin-top:16px;text-align:center;font-size:10px;color:#94a3b8}}
  @media print{{@page{{margin:10mm}} button{{display:none}}}}
</style></head><body>
<div class="header">
  <div class="shop">
    <div class="shop-name">{nom_depot}</div>
    <div class="shop-sub">{adresse} · {tel}</div>
  </div>
  <div class="ref-block">
    <div class="ref">{t['reference']}</div>
    <div class="badge">{st[0]} {st[2]}</div>
    <div style="font-size:10px;color:#94a3b8;margin-top:4px;">Reçu le {t['date_reception']}</div>
  </div>
</div>

<div class="two">
  <div class="card">
    <div class="card-title">👤 Client</div>
    <div class="row"><span class="rlabel">Nom</span><span class="rval">{t['client_nom']}</span></div>
    <div class="row"><span class="rlabel">Tél</span><span class="rval">{t['client_tel'] or '—'}</span></div>
    <div class="row"><span class="rlabel">Adresse</span><span class="rval">{t['client_adresse'] or '—'}</span></div>
  </div>
  <div class="card">
    <div class="card-title">💻 Appareil</div>
    <div class="row"><span class="rlabel">Équipement</span><span class="rval">{appareil_info or '—'}</span></div>
    <div class="row"><span class="rlabel">Technicien</span><span class="rval">{t['technicien_nom'] or '—'}</span></div>
    <div class="row"><span class="rlabel">Priorité</span><span class="rval" style="color:{pr[0]}">{pr[1]} {(t['priorite'] or '').capitalize()}</span></div>
    <div class="row"><span class="rlabel">Date prévue</span><span class="rval">{t['date_prevue'] or '—'}</span></div>
  </div>
</div>

<div class="card" style="margin-bottom:12px;">
  <div class="card-title">🔧 Panne &amp; Diagnostic</div>
  <div class="row"><span class="rlabel">Type de panne</span><span class="rval">{t['type_panne'] or '—'}</span></div>
  <div class="row"><span class="rlabel">Description</span><span class="rval">{t['description_panne'] or '—'}</span></div>
  <div class="row"><span class="rlabel">Accessoires reçus</span><span class="rval">{t['accessoires'] or '—'}</span></div>
  <div class="row"><span class="rlabel">Mot de passe</span><span class="rval">{t['mot_de_passe'] or '—'}</span></div>
  <div class="row"><span class="rlabel">Diagnostic</span><span class="rval">{t['diagnostic'] or '—'}</span></div>
  <div class="row"><span class="rlabel">Travaux effectués</span><span class="rval">{t['travaux_effectues'] or '—'}</span></div>
</div>

<div class="two">
  <div class="card">
    <div class="card-title">💰 Facturation</div>
    <div class="row"><span class="rlabel">Coût estimé</span><span class="rval">{(t['cout_estime'] or 0):,.0f} {devise}</span></div>
    <div class="row"><span class="rlabel">Coût final</span><span class="rval">{(t['cout_final'] or 0):,.0f} {devise}</span></div>
    <div class="row"><span class="rlabel">Déjà réglé</span><span class="rval" style="color:#16a34a">{regle:,.0f} {devise}</span></div>
    <div class="row total-row"><span class="rlabel">Reste à payer</span><span class="rval" style="color:{'#dc2626' if reste>0 else '#16a34a'}">{reste:,.0f} {devise}</span></div>
  </div>
  <div class="card">
    <div class="card-title">📋 Règlements</div>
    <table>
      <thead><tr><th>Date</th><th>Mode</th><th>Montant</th></tr></thead>
      <tbody>{rgl_rows}</tbody>
    </table>
  </div>
</div>

<div class="footer">
  Document généré le {date.today().isoformat()} · {nom_depot}<br>
  <em>Merci de votre confiance</em>
</div>
<br><button onclick="window.print()" style="padding:8px 20px;background:#2563eb;color:white;border:none;border-radius:6px;cursor:pointer;font-size:12px;">🖨️ Imprimer</button>
<script>window.addEventListener('load',function(){{setTimeout(function(){{window.print();}},250);}});</script>
</body></html>"""

# ══════════════════════════════════════════════════════════════════════
#  GESTIONNAIRES D'ERREURS HTTP
# ══════════════════════════════════════════════════════════════════════

def _error_page(code, titre, message, description=''):
    """Page HTML autonome pour les erreurs HTTP (navigation directe)."""
    icons = {400:'⚠️', 403:'⛔', 404:'🔍', 500:'🔥'}
    bars  = {400:'#f59e0b,#f97316', 403:'#7c3aed,#dc2626',
             404:'#0ea5e9,#6366f1', 500:'#dc2626,#f97316'}
    icon = icons.get(code, '🔥')
    bar  = bars.get(code, '#dc2626,#f97316')
    desc_block = f"<div class='desc'>{description}</div>" if description else ''
    html = f"""<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Erreur {code} — DISTRIGEST</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;background:#f1f5f9;
       display:flex;align-items:center;justify-content:center;
       min-height:100vh;padding:24px}}
  .card{{background:#fff;border-radius:18px;box-shadow:0 12px 40px rgba(0,0,0,.12);
         max-width:480px;width:100%;overflow:hidden;animation:cardIn .28s cubic-bezier(.34,1.4,.64,1)}}
  @keyframes cardIn{{from{{opacity:0;transform:translateY(20px)}}to{{opacity:1;transform:none}}}}
  .topbar{{height:5px;background:linear-gradient(90deg,{bar})}}
  .body{{padding:36px 40px 30px;text-align:center}}
  .icon{{font-size:50px;margin-bottom:12px;line-height:1}}
  .code{{font-size:64px;font-weight:800;line-height:1;
         background:linear-gradient(135deg,{bar});
         -webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:6px}}
  .titre{{font-size:18px;font-weight:700;color:#1e293b;margin-bottom:8px}}
  .msg{{font-size:13px;color:#64748b;line-height:1.6;margin-bottom:6px}}
  .desc{{font-size:11.5px;color:#94a3b8;font-style:italic;margin-bottom:20px}}
  .actions{{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-top:24px}}
  .btn{{padding:10px 20px;border-radius:9px;border:none;cursor:pointer;
        font-size:13px;font-weight:600;text-decoration:none;display:inline-block}}
  .btn-ghost{{background:#f1f5f9;color:#475569}}
  .btn-back{{background:#e2e8f0;color:#334155}}
  .btn-home{{background:#2563eb;color:#fff}}
</style></head><body>
<div class="card">
  <div class="topbar"></div>
  <div class="body">
    <div class="icon">{icon}</div>
    <div class="code">{code}</div>
    <div class="titre">{titre}</div>
    <div class="msg">{message}</div>
    {desc_block}
    <div class="actions">
      <button class="btn btn-ghost" onclick="window.close()">✕ Fermer</button>
      <button class="btn btn-back"  onclick="history.back()">← Retour</button>
      <a      class="btn btn-home"  href="/">🏠 Accueil</a>
    </div>
  </div>
</div>
</body></html>"""
    return html, code


# ══════════════════════════════════════════════════════════════════════
#  GESTION CENTRALISÉE DES ERREURS HTTP — template unique erreur.html
# ══════════════════════════════════════════════════════════════════════
_ERREURS_HTTP = {
    400: ('⚠️', '#f59e0b', 'Requête invalide',
          "Les données envoyées sont incorrectes ou incomplètes."),
    401: ('🔑', '#0ea5e9', 'Authentification requise',
          "Connectez-vous pour accéder à cette page."),
    403: ('⛔', '#7c3aed', 'Accès refusé',
          "Vous n'avez pas les droits nécessaires pour accéder à cette ressource."),
    404: ('🔍', '#3b82f6', 'Page introuvable',
          "La ressource demandée n'existe pas ou a été déplacée."),
    405: ('🚫', '#f97316', 'Méthode non autorisée',
          "Cette action n'est pas permise sur cette adresse."),
    408: ('⏱️', '#f59e0b', 'Délai dépassé',
          "Le serveur a mis trop de temps à répondre. Réessayez."),
    413: ('📦', '#f59e0b', 'Fichier trop volumineux',
          "Le fichier envoyé dépasse la taille maximale autorisée."),
    429: ('🐢', '#f59e0b', 'Trop de requêtes',
          "Trop de requêtes en peu de temps. Patientez un instant puis réessayez."),
    500: ('🔥', '#dc2626', 'Erreur interne du serveur',
          "Une erreur inattendue s'est produite. "
          "Si le problème persiste, contactez le support STiNAUG TECHNOLOGIE."),
    502: ('🔌', '#dc2626', 'Passerelle défaillante',
          "Le serveur a reçu une réponse invalide. Réessayez dans un instant."),
    503: ('🛠️', '#dc2626', 'Service indisponible',
          "Le service est momentanément indisponible (maintenance ou surcharge)."),
}

def _http_error_response(code, e=None, detail=''):
    """Réponse d'erreur unifiée :
       • client JSON (AJAX/API)  → corps JSON
       • navigateur              → template erreur.html (toutes erreurs)
       • repli ultime            → _error_page() (HTML en dur) si le
                                    dossier templates est lui-même cassé."""
    icone, couleur, titre, message = _ERREURS_HTTP.get(
        code, ('🔥', '#dc2626', f'Erreur {code}', "Une erreur s'est produite."))

    # Détail éventuel porté par l'exception (description Werkzeug)
    if not detail and e is not None and getattr(e, 'description', None):
        d = str(e.description)
        # Ne pas afficher les descriptions techniques par défaut de Werkzeug
        if d and not d.startswith('The ') and 'browser' not in d:
            detail = d

    # Référence d'incident pour les erreurs serveur (corrélation avec les logs)
    ref = None
    if code >= 500:
        import uuid as _uuid
        ref = _uuid.uuid4().hex[:8].upper()
        logging.error("[ERREUR %s] ref=%s url=%s : %s", code, ref,
                      getattr(request, 'path', '?'), e)

    # Clients JSON (fetch/AJAX/API)
    try:
        if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
            return jsonify(ok=False, code=code, error=titre,
                           message=detail or message, ref=ref), code
    except Exception:
        pass

    # Page HTML via le template central
    try:
        return render_template('erreur.html', code=code, icone=icone,
                               couleur=couleur, titre=titre, message=message,
                               detail=detail, ref=ref), code
    except Exception as exc_tpl:
        logging.warning("[ERREUR] template erreur.html indisponible (%s) — repli inline", exc_tpl)
        return _error_page(code, titre, message, detail or '')


# Enregistrement de tous les codes HTTP gérés
for _code in _ERREURS_HTTP:
    def _make_handler(c):
        def _h(e):
            if c == 500:
                import traceback as _tb
                logging.error("Erreur 500 : %s\n%s", e, _tb.format_exc())
            return _http_error_response(c, e)
        return _h
    app.register_error_handler(_code, _make_handler(_code))

# Filet de sécurité : exceptions non interceptées → page 500 propre
@app.errorhandler(Exception)
def err_exception(e):
    from werkzeug.exceptions import HTTPException as _HTTPExc
    if isinstance(e, _HTTPExc):
        # Code HTTP non listé ci-dessus (ex. 410, 501…) → page générique
        return _http_error_response(e.code or 500, e)
    import traceback as _tb
    logging.error("Exception non gérée : %s\n%s", e, _tb.format_exc())
    return _http_error_response(500, e)


# ══════════════════════════════════════════════════════════════════════
#  MODULE ATELIER — CONVERTIR GESTIT → DISTRIGEST
# ══════════════════════════════════════════════════════════════════════
@app.route('/atelier/convertir', methods=['GET', 'POST'])
@login_required
def atelier_convertir():
    """Module de conversion GESTIT → DISTRIGEST.
    Importe les données d'une base gestit.db dans distrigest.db.
    Tables converties :
      gestit.clients      → distrigest.clients
      gestit.fournisseurs → distrigest.fournisseurs
      gestit.familles     → distrigest.familles
      gestit.stock        → distrigest.articles + stocks
      gestit.depots       → distrigest.depots
      gestit.employes     → distrigest.employes
      gestit.equipements  → distrigest.equipements
      gestit.tickets      → distrigest.tickets
      gestit.depenses     → distrigest.depenses
      gestit.config       → distrigest.parametres (clés communes)
    """
    if session.get('user_role') != 'admin':
        flash("Accès réservé à l'administrateur.", "danger")
        return redirect(url_for('dashboard'))

    cfg = get_cfg()
    rapport = None

    if request.method == 'POST':
        import tempfile, os as _os
        fichier = request.files.get('fichier_gestit')
        if not fichier or not fichier.filename.endswith('.db'):
            flash("Veuillez sélectionner un fichier .db valide (base GESTIT).", "danger")
            return redirect(url_for('atelier_convertir'))

        # Sauvegarder temporairement
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        fichier.save(tmp.name)
        tmp.close()

        rapport = None
        try:
            rapport = _convertir_gestit(tmp.name)
        except Exception as _e:
            logging.error("[CONVERTIR] Erreur : %s", _e)
            flash(f"Erreur lors de la conversion : {_e}", "danger")
        finally:
            try:
                _os.unlink(tmp.name)
            except Exception:
                pass  # Windows : fichier parfois encore verrouillé, on ignore

        if rapport and rapport['erreurs'] == 0:
            flash("✅ Conversion terminée avec succès !", "success")
        elif rapport:
            flash(f"⚠️ Conversion terminée avec {rapport['erreurs']} avertissement(s).", "warning")

    return render_template('atelier_convertir.html', cfg=cfg, rapport=rapport)


@app.route('/atelier/exporter')
@login_required
def atelier_exporter():
    """Exporte une copie complète de la base DISTRIGEST active (fichier .db).

    Utilisé par le bouton « Exporter la base » de la page de conversion.
    Effectue une sauvegarde cohérente via l'API backup de SQLite (sûr même
    si la base est en cours d'utilisation), puis renvoie le fichier.
    """
    if session.get('user_role') != 'admin':
        flash("Accès réservé à l'administrateur.", "danger")
        return redirect(url_for('dashboard'))

    import os as _os, tempfile, sqlite3 as _sq
    from flask import send_file

    export_name = f"distrigest_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"

    # Sauvegarde cohérente vers un fichier temporaire (évite tout risque de
    # corruption si une écriture est en cours sur la base active).
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    tmp.close()
    try:
        src = _sq.connect(DB_PATH)
        dst = _sq.connect(tmp.name)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
    except Exception as _e:
        logging.error("[EXPORT] Erreur lors de la sauvegarde : %s", _e)
        # Repli : envoi direct du fichier de base si la copie échoue.
        try:
            _os.unlink(tmp.name)
        except Exception:
            pass
        return send_file(DB_PATH, as_attachment=True, download_name=export_name)

    return send_file(tmp.name, as_attachment=True, download_name=export_name)


def _convertir_gestit(gestit_path):
    """Effectue la conversion complète gestit.db → distrigest.db.
    Retourne un dict rapport avec les compteurs par table."""
    import sqlite3 as _sq

    rapport = {
        'tables': [],
        'erreurs': 0,
        'total_importe': 0,
    }

    def _log(table, nb, skipped=0, msg=''):
        rapport['tables'].append({
            'table': table, 'importe': nb, 'skipped': skipped, 'msg': msg
        })
        rapport['total_importe'] += nb

    src = _sq.connect(gestit_path)
    src.row_factory = _sq.Row

    def _row(r):
        """Convertit sqlite3.Row en dict (compatible .get())."""
        return dict(r)

    # ── Vérification basique du schéma source ──────────────────────
    tables_src = {r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    # Tables réellement indispensables. 'depots' est optionnel : GESTIT ne
    # gère pas toujours de dépôts, et la conversion du stock utilise de toute
    # façon le dépôt par défaut de DISTRIGEST, pas ceux de GESTIT.
    tables_attendues = {'clients', 'stock', 'tickets', 'equipements'}
    manquantes = tables_attendues - tables_src
    if manquantes:
        src.close()
        raise ValueError(f"Base GESTIT invalide — tables manquantes : {manquantes}")

    db = get_db()

    # ────────────────────────────────────────────────────────────────
    # 1. CONFIG → parametres (clés communes)
    # ────────────────────────────────────────────────────────────────
    mapping_config = {
        'entreprise_nom':      'nom_depot',
        'adresse':             'adresse',
        'telephone':           'telephone',
        'email':               'email',
        'devise':              'devise',
        'tva':                 'tva_defaut',
        'entreprise_adresse':  'adresse',
        'entreprise_tel':      'telephone',
        'entreprise_email':    'email',
    }
    nb_cfg = 0
    if 'config' in tables_src:
        for row in src.execute("SELECT cle, valeur FROM config"):
            row = _row(row)
            cle_dst = mapping_config.get(row['cle'])
            if cle_dst and row['valeur']:
                db.execute(
                    "INSERT OR IGNORE INTO parametres(cle,valeur) VALUES(?,?)",
                    (cle_dst, row['valeur']))
                nb_cfg += 1
        db.commit()
    _log('config → parametres', nb_cfg)

    # ────────────────────────────────────────────────────────────────
    # 2. FAMILLES
    # ────────────────────────────────────────────────────────────────
    nb_fam = nb_skip_fam = 0
    if 'familles' in tables_src:
        for row in src.execute("SELECT * FROM familles"):
            row = _row(row)
            existing = db.execute(
                "SELECT id FROM familles WHERE nom=?", (row['nom'],)).fetchone()
            if existing:
                nb_skip_fam += 1
                continue
            try:
                db.execute(
                    "INSERT INTO familles(nom, couleur) VALUES(?,?)",
                    (row['nom'], row.get('couleur') or '#2563eb'))
                nb_fam += 1
            except Exception:
                nb_skip_fam += 1
        db.commit()
    _log('familles', nb_fam, nb_skip_fam)

    # ────────────────────────────────────────────────────────────────
    # 3. DÉPÔTS
    # ────────────────────────────────────────────────────────────────
    depot_id_map = {}   # gestit_id → distrigest_id
    nb_dep = nb_skip_dep = 0
    if 'depots' in tables_src:
        for row in src.execute("SELECT * FROM depots"):
            row = _row(row)
            existing = db.execute(
                "SELECT id FROM depots WHERE nom=?", (row['nom'],)).fetchone()
            if existing:
                depot_id_map[row['id']] = existing['id']
                nb_skip_dep += 1
                continue
            code = (row.get('code') or row['nom'][:6].upper().replace(' ', '_'))
            existing_code = db.execute(
                "SELECT id FROM depots WHERE code=?", (code,)).fetchone()
            if existing_code:
                code = code + str(row['id'])
            try:
                db.execute(
                    "INSERT INTO depots(code, nom, adresse, responsable, actif) VALUES(?,?,?,?,?)",
                    (code, row['nom'],
                     row.get('adresse') or '',
                     row.get('responsable') or '',
                     row.get('actif', 1)))
                new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                depot_id_map[row['id']] = new_id
                nb_dep += 1
            except Exception as _e:
                nb_skip_dep += 1
                rapport['erreurs'] += 1
        db.commit()
    _log('depots', nb_dep, nb_skip_dep,
         msg='' if 'depots' in tables_src else 'Aucun dépôt dans GESTIT — dépôt par défaut utilisé')

    # ────────────────────────────────────────────────────────────────
    # 4. FOURNISSEURS
    # ────────────────────────────────────────────────────────────────
    nb_fou = nb_skip_fou = 0
    if 'fournisseurs' in tables_src:
        n_fou = db.execute("SELECT COUNT(*) FROM fournisseurs").fetchone()[0]
        for row in src.execute("SELECT * FROM fournisseurs"):
            row = _row(row)
            existing = db.execute(
                "SELECT id FROM fournisseurs WHERE nom=?", (row['nom'],)).fetchone()
            if existing:
                nb_skip_fou += 1
                continue
            n_fou += 1
            code = f"FOU{n_fou:03d}"
            try:
                db.execute("""INSERT INTO fournisseurs(code, nom, contact, telephone, email,
                               adresse, ville, pays, type_produits, delai_livraison,
                               conditions_paiement, notes, actif)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (code,
                            (row['nom'] or '').upper(),
                            row.get('contact') or '',
                            row.get('telephone') or '',
                            row.get('email') or '',
                            row.get('adresse') or '',
                            row.get('ville') or 'Abidjan',
                            row.get('pays') or "Côte d'Ivoire",
                            row.get('type_produits') or '',
                            row.get('delai_livraison') or '',
                            row.get('conditions_paiement') or '',
                            row.get('notes') or '',
                            row.get('actif', 1)))
                nb_fou += 1
            except Exception:
                nb_skip_fou += 1
                rapport['erreurs'] += 1
        db.commit()
    _log('fournisseurs', nb_fou, nb_skip_fou)

    # ────────────────────────────────────────────────────────────────
    # 5. CLIENTS
    # ────────────────────────────────────────────────────────────────
    client_id_map = {}  # gestit_id → distrigest_id
    nb_cli = nb_skip_cli = 0
    n_cli = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    for row in src.execute("SELECT * FROM clients"):
        row = _row(row)
        existing = db.execute(
            "SELECT id FROM clients WHERE telephone=? AND nom=?",
            (row.get('telephone') or '', row['nom'])).fetchone()
        if existing:
            client_id_map[row['id']] = existing['id']
            nb_skip_cli += 1
            continue
        n_cli += 1
        code = f"CLI{n_cli:04d}"
        while db.execute("SELECT id FROM clients WHERE code=?", (code,)).fetchone():
            n_cli += 1
            code = f"CLI{n_cli:04d}"
        try:
            db.execute("""INSERT INTO clients(code, nom, prenom, type_client, telephone,
                           email, adresse, ville, notes)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                       (code,
                        (row['nom'] or '').upper(),
                        row.get('prenom') or '',
                        row.get('type_client') or 'particulier',
                        row.get('telephone') or '',
                        row.get('email') or '',
                        row.get('adresse') or '',
                        'Abidjan',
                        row.get('notes') or ''))
            new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            client_id_map[row['id']] = new_id
            nb_cli += 1
        except Exception:
            nb_skip_cli += 1
            rapport['erreurs'] += 1
    db.commit()
    _log('clients', nb_cli, nb_skip_cli)

    # ────────────────────────────────────────────────────────────────
    # 6. ARTICLES / STOCK (table stock de gestit)
    # ────────────────────────────────────────────────────────────────
    article_id_map = {}  # gestit_stock.id → distrigest.articles.id
    nb_art = nb_skip_art = 0
    n_art = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]

    # Mapper les familles par nom
    fam_map = {r['nom']: r['id'] for r in db.execute("SELECT id, nom FROM familles")}

    default_depot_id = db.execute(
        "SELECT id FROM depots WHERE actif=1 ORDER BY id LIMIT 1").fetchone()
    default_depot_id = default_depot_id['id'] if default_depot_id else None

    for row in src.execute("SELECT * FROM stock"):
        row = _row(row)
        existing = db.execute(
            "SELECT id FROM articles WHERE designation=?", (row['designation'],)).fetchone()
        if existing:
            article_id_map[row['id']] = existing['id']
            nb_skip_art += 1
            continue
        n_art += 1
        ref = row.get('reference') or f"ART{n_art:04d}"
        # Éviter doublon de référence
        while db.execute("SELECT id FROM articles WHERE reference=?", (ref,)).fetchone():
            n_art += 1
            ref = f"ART{n_art:04d}"

        famille_id = None
        if row.get('famille_id') and 'familles' in tables_src:
            # Chercher famille correspondante par id→nom dans gestit
            fam_row = src.execute(
                "SELECT nom FROM familles WHERE id=?", (row['famille_id'],)).fetchone()
            if fam_row:
                famille_id = fam_map.get(fam_row['nom'])
        elif row.get('categorie'):
            famille_id = fam_map.get(row['categorie'])

        try:
            db.execute("""INSERT INTO articles(reference, designation, famille_id,
                           contenance, colisage, unite_vente,
                           prix_achat_ht, prix_vente_ht, prix_unitaire,
                           tva, code_barre, notes, icone, actif)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                       (ref,
                        row['designation'],
                        famille_id,
                        row.get('contenance') or '',
                        int(row.get('colisage') or 1),
                        row.get('unite_vente') or row.get('unites_vente') or 'Unité',
                        float(row.get('prix_achat_ht') or row.get('prix_achat') or 0),
                        float(row.get('prix_vente_ht') or row.get('prix_vente') or 0),
                        float(row.get('prix_unitaire') or 0),
                        float(row.get('tva') or 0),
                        row.get('code_barre') or None,
                        row.get('notes') or '',
                        row.get('icone') or '📦'))
            new_art_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            article_id_map[row['id']] = new_art_id

            # Stock initial → dans tous les dépôts
            depots_dst = [r['id'] for r in db.execute("SELECT id FROM depots WHERE actif=1")]
            for dep_id in depots_dst:
                qte = 0.0
                if dep_id == default_depot_id:
                    # Chercher dans stock_depots si dispo
                    sd = None
                    if 'stock_depots' in tables_src:
                        sd = src.execute(
                            "SELECT quantite_unite FROM stock_depots WHERE article_id=? LIMIT 1",
                            (row['id'],)).fetchone()
                    if sd:
                        qte = float(sd['quantite_unite'] or 0)
                    else:
                        qte = float(row.get('quantite') or 0)
                db.execute("""INSERT OR IGNORE INTO stocks(article_id, depot_id, quantite_unite)
                               VALUES(?,?,?)""", (new_art_id, dep_id, qte))
            nb_art += 1
        except Exception as _e:
            nb_skip_art += 1
            rapport['erreurs'] += 1

    db.commit()
    _log('articles / stock', nb_art, nb_skip_art)

    # ────────────────────────────────────────────────────────────────
    # 7. EMPLOYÉS
    # ────────────────────────────────────────────────────────────────
    employe_id_map = {}
    nb_emp = nb_skip_emp = 0
    if 'employes' in tables_src:
        n_emp = db.execute("SELECT COUNT(*) FROM employes").fetchone()[0]
        for row in src.execute("SELECT * FROM employes"):
            row = _row(row)
            existing = db.execute(
                "SELECT id FROM employes WHERE nom=? AND telephone=?",
                (row['nom'], row.get('telephone') or '')).fetchone()
            if existing:
                employe_id_map[row['id']] = existing['id']
                nb_skip_emp += 1
                continue
            n_emp += 1
            mat = row.get('matricule') or f"EMP{n_emp:03d}"
            while db.execute("SELECT id FROM employes WHERE matricule=?", (mat,)).fetchone():
                n_emp += 1
                mat = f"EMP{n_emp:03d}"
            try:
                db.execute("""INSERT INTO employes(matricule, nom, prenom, poste,
                               telephone, email, salaire_base, date_embauche, statut, notes)
                               VALUES(?,?,?,?,?,?,?,?,?,?)""",
                           (mat,
                            (row['nom'] or '').upper(),
                            row.get('prenom') or '',
                            row.get('poste') or 'Technicien',
                            row.get('telephone') or '',
                            row.get('email') or '',
                            float(row.get('salaire_base') or 0),
                            row.get('date_embauche') or None,
                            row.get('statut') or 'actif',
                            row.get('notes') or ''))
                new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                employe_id_map[row['id']] = new_id
                nb_emp += 1
            except Exception:
                nb_skip_emp += 1
                rapport['erreurs'] += 1
        db.commit()
    _log('employes', nb_emp, nb_skip_emp)

    # ────────────────────────────────────────────────────────────────
    # 8. ÉQUIPEMENTS
    # ────────────────────────────────────────────────────────────────
    equip_id_map = {}
    nb_eq = nb_skip_eq = 0
    for row in src.execute("SELECT * FROM equipements"):
        row = _row(row)
        client_id_dst = client_id_map.get(row['client_id'])
        if not client_id_dst:
            nb_skip_eq += 1
            continue
        existing = db.execute(
            "SELECT id FROM equipements WHERE client_id=? AND numero_serie=?",
            (client_id_dst, row.get('numero_serie') or '')).fetchone()
        if existing and row.get('numero_serie'):
            equip_id_map[row['id']] = existing['id']
            nb_skip_eq += 1
            continue
        try:
            db.execute("""INSERT INTO equipements(client_id, type_appareil, marque,
                           modele, numero_serie, couleur, description, date_creation)
                           VALUES(?,?,?,?,?,?,?,?)""",
                       (client_id_dst,
                        row.get('type_appareil') or 'Autre',
                        row.get('marque') or '',
                        row.get('modele') or '',
                        row.get('numero_serie') or '',
                        row.get('couleur') or '',
                        row.get('description') or '',
                        row.get('created_at') or date.today().isoformat()))
            new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            equip_id_map[row['id']] = new_id
            nb_eq += 1
        except Exception:
            nb_skip_eq += 1
            rapport['erreurs'] += 1
    db.commit()
    _log('equipements', nb_eq, nb_skip_eq)

    # ────────────────────────────────────────────────────────────────
    # 9. TICKETS DE RÉPARATION
    # ────────────────────────────────────────────────────────────────
    nb_tkt = nb_skip_tkt = 0
    for row in src.execute("SELECT * FROM tickets"):
        row = _row(row)
        client_id_dst = client_id_map.get(row['client_id'])
        if not client_id_dst:
            nb_skip_tkt += 1
            continue
        # Éviter les doublons par référence
        ref_src = row.get('reference') or ''
        if ref_src:
            existing = db.execute(
                "SELECT id FROM tickets WHERE reference=?", (ref_src,)).fetchone()
            if existing:
                nb_skip_tkt += 1
                continue
        equip_id_dst = equip_id_map.get(row.get('equipement_id')) if row.get('equipement_id') else None
        tech_id_dst  = employe_id_map.get(row.get('technicien_id')) if row.get('technicien_id') else None

        # Mapper statut_paiement gestit → champs distrigest
        statut_paiement = row.get('statut_paiement') or 'non_regle'
        montant_regle = float(row.get('montant_regle') or 0)

        try:
            db.execute("""INSERT INTO tickets(reference, client_id, equipement_id,
                           type_panne, description_panne, accessoires, mot_de_passe,
                           statut, priorite, technicien_id,
                           date_reception, date_prevue, date_cloture,
                           diagnostic, travaux_effectues,
                           cout_estime, cout_final, montant_regle, notes, date_creation)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (ref_src or None,
                        client_id_dst,
                        equip_id_dst,
                        row.get('type_panne') or '',
                        row.get('description_panne') or '',
                        row.get('accessoires') or '',
                        row.get('mot_de_passe') or '',
                        row.get('statut') or 'recu',
                        row.get('priorite') or 'normale',
                        tech_id_dst,
                        row.get('date_reception') or date.today().isoformat(),
                        row.get('date_prevue') or None,
                        row.get('date_cloture') or None,
                        row.get('diagnostic') or '',
                        row.get('travaux_effectues') or '',
                        float(row.get('cout_estime') or 0),
                        float(row.get('cout_final') or 0),
                        montant_regle,
                        row.get('notes') or '',
                        row.get('created_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            nb_tkt += 1
        except Exception as _e:
            nb_skip_tkt += 1
            rapport['erreurs'] += 1
    db.commit()
    _log('tickets', nb_tkt, nb_skip_tkt)

    # ────────────────────────────────────────────────────────────────
    # 10. DÉPENSES
    # ────────────────────────────────────────────────────────────────
    nb_dep_d = nb_skip_dep_d = 0
    if 'depenses' in tables_src:
        for row in src.execute("SELECT * FROM depenses"):
            row = _row(row)
            # Doublon : même description + date + montant
            existing = db.execute(
                "SELECT id FROM depenses WHERE description=? AND date_depense=? AND montant=?",
                (row.get('description') or '', row.get('date_depense') or '',
                 float(row.get('montant') or 0))).fetchone()
            if existing:
                nb_skip_dep_d += 1
                continue
            try:
                db.execute("""INSERT INTO depenses(categorie, description, montant,
                               date_depense, responsable, notes)
                               VALUES(?,?,?,?,?,?)""",
                           (row.get('categorie') or 'Autre',
                            row.get('description') or '',
                            float(row.get('montant') or 0),
                            row.get('date_depense') or date.today().isoformat(),
                            row.get('responsable') or '',
                            row.get('notes') or ''))
                nb_dep_d += 1
            except Exception:
                nb_skip_dep_d += 1
                rapport['erreurs'] += 1
        db.commit()
    _log('depenses', nb_dep_d, nb_skip_dep_d)


    # ────────────────────────────────────────────────────────────────
    # 11. VENTES GESTIT → documents_vente (type facture) + lignes_vente
    # ────────────────────────────────────────────────────────────────
    vente_id_map = {}   # gestit vente.id → distrigest documents_vente.id
    nb_vte = nb_skip_vte = 0
    if 'ventes' in tables_src:
        for row in src.execute("SELECT * FROM ventes ORDER BY id"):
            row = _row(row)
            ref = row.get('reference') or ''
            if ref and db.execute("SELECT id FROM documents_vente WHERE reference=?", (ref,)).fetchone():
                nb_skip_vte += 1
                continue
            client_id_dst = client_id_map.get(row.get('client_id'))
            ttc = float(row.get('montant_ttc') or 0)
            tva_pct = float(row.get('tva') or 0)
            ht  = round(ttc / (1 + tva_pct / 100), 2) if tva_pct else ttc
            tva_mt = round(ttc - ht, 2)
            paye = float(row.get('montant_regle') or 0)
            reste = round(max(0, ttc - paye), 2)
            statut_src = (row.get('statut') or 'en_attente').lower()
            statut_map_v = {
                'payee': 'reglee', 'paye': 'reglee', 'regle': 'reglee',
                'partiel': 'partielle', 'partielle': 'partielle',
                'annulee': 'annule', 'annule': 'annule',
            }
            statut_dst = statut_map_v.get(statut_src, 'en_attente')
            depot_id_dst = default_depot_id
            try:
                db.execute("""INSERT INTO documents_vente(
                               type_doc, reference, client_id, depot_id,
                               date_doc, statut,
                               total_ht, total_tva, total_ttc,
                               montant_paye, reste, mode_paiement, notes)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           ('facture', ref or None, client_id_dst, depot_id_dst,
                            row.get('date_vente') or date.today().isoformat(),
                            statut_dst,
                            ht, tva_mt, ttc,
                            paye, reste,
                            row.get('mode_paiement') or 'especes',
                            row.get('notes') or ''))
                new_vte_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                vente_id_map[row['id']] = new_vte_id
                nb_vte += 1

                # ── Lignes de vente ──────────────────────────────────
                if 'vente_lignes' in tables_src:
                    for lrow in src.execute(
                            "SELECT * FROM vente_lignes WHERE vente_id=? ORDER BY id",
                            (row['id'],)):
                        lrow = _row(lrow)
                        art_id_dst = article_id_map.get(lrow.get('stock_id'))
                        qte = float(lrow.get('quantite') or 0)
                        pu_ht = float(lrow.get('prix_unitaire') or 0)
                        rem = float(lrow.get('remise_pct') or 0)
                        tva_l = float(lrow.get('tva_pct') or 0)
                        l_ht  = float(lrow.get('montant_ht') or round(qte * pu_ht * (1 - rem/100), 2))
                        l_ttc = float(lrow.get('montant_ttc') or lrow.get('total_ligne') or l_ht)
                        try:
                            db.execute("""INSERT INTO lignes_vente(
                                           document_id, article_id, designation,
                                           quantite_unite, quantite_colis,
                                           prix_ht, remise_pct, tva,
                                           total_ht, total_ttc)
                                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                       (new_vte_id, art_id_dst,
                                        lrow.get('designation') or lrow.get('code_article') or '',
                                        qte, 0, pu_ht, rem, tva_l, l_ht, l_ttc))
                        except Exception:
                            pass

            except Exception:
                nb_skip_vte += 1
                rapport['erreurs'] += 1
        db.commit()
    _log('ventes → documents_vente + lignes', nb_vte, nb_skip_vte)

    # ────────────────────────────────────────────────────────────────
    # 12. FACTURES ATELIER GESTIT → documents_vente (type facture)
    #     (factures liées aux tickets, distinctes des ventes directes)
    # ────────────────────────────────────────────────────────────────
    facture_id_map = {}
    nb_fac = nb_skip_fac = 0
    if 'factures' in tables_src:
        for row in src.execute("SELECT * FROM factures ORDER BY id"):
            row = _row(row)
            ref = row.get('reference') or ''
            if ref and db.execute("SELECT id FROM documents_vente WHERE reference=?", (ref,)).fetchone():
                nb_skip_fac += 1
                continue
            # Si la vente associée a déjà été importée, ne pas dupliquer
            if row.get('vente_id') and row['vente_id'] in vente_id_map:
                facture_id_map[row['id']] = vente_id_map[row['vente_id']]
                nb_skip_fac += 1
                continue
            client_id_dst = client_id_map.get(row.get('client_id'))
            ttc = float(row.get('montant_ttc') or 0)
            tva_pct = float(row.get('tva') or 0)
            ht  = round(ttc / (1 + tva_pct / 100), 2) if tva_pct else ttc
            tva_mt = round(ttc - ht, 2)
            paye = float(row.get('montant_regle') or 0)
            reste = round(max(0, ttc - paye), 2)
            statut_map_f = {
                'payee': 'reglee', 'paye': 'reglee', 'regle': 'reglee',
                'partiel': 'partielle', 'partielle': 'partielle',
                'annulee': 'annule',
            }
            statut_dst = statut_map_f.get((row.get('statut') or '').lower(), 'en_attente')
            # Lignes stockées en JSON dans gestit (champ 'lignes')
            lignes_json = []
            try:
                import json as _json
                if row.get('lignes'):
                    lignes_json = _json.loads(row['lignes']) if isinstance(row['lignes'], str) else row['lignes']
            except Exception:
                lignes_json = []
            try:
                db.execute("""INSERT INTO documents_vente(
                               type_doc, reference, client_id, depot_id,
                               date_doc, statut,
                               total_ht, total_tva, total_ttc,
                               montant_paye, reste, mode_paiement, notes)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           ('facture', ref or None, client_id_dst, default_depot_id,
                            row.get('date_emission') or date.today().isoformat(),
                            statut_dst,
                            ht, tva_mt, ttc,
                            paye, reste,
                            row.get('mode_paiement') or 'especes',
                            row.get('notes') or ''))
                new_fac_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                facture_id_map[row['id']] = new_fac_id
                # Injecter les lignes depuis le JSON
                for i, l in enumerate(lignes_json or [], 1):
                    if not isinstance(l, dict):
                        continue
                    art_id_dst = article_id_map.get(l.get('stock_id') or l.get('article_id'))
                    qte  = float(l.get('quantite') or 1)
                    pu   = float(l.get('prix_unitaire') or l.get('prix_ht') or 0)
                    rem  = float(l.get('remise_pct') or 0)
                    tva_l = float(l.get('tva_pct') or tva_pct)
                    l_ht  = round(qte * pu * (1 - rem/100), 2)
                    l_tva = round(l_ht * tva_l / 100, 2)
                    l_ttc = l_ht + l_tva
                    try:
                        db.execute("""INSERT INTO lignes_vente(
                                       document_id, article_id, designation,
                                       quantite_unite, prix_ht, remise_pct, tva,
                                       total_ht, total_ttc, num_ligne)
                                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                   (new_fac_id, art_id_dst,
                                    l.get('designation') or l.get('libelle') or '',
                                    qte, pu, rem, tva_l, l_ht, l_ttc, i))
                    except Exception:
                        pass
                nb_fac += 1
            except Exception:
                nb_skip_fac += 1
                rapport['erreurs'] += 1
        db.commit()
    _log('factures atelier → documents_vente', nb_fac, nb_skip_fac)

    # ────────────────────────────────────────────────────────────────
    # 13. DEVIS GESTIT → documents_vente (type devis)
    # ────────────────────────────────────────────────────────────────
    nb_dev = nb_skip_dev = 0
    if 'devis' in tables_src:
        for row in src.execute("SELECT * FROM devis ORDER BY id"):
            row = _row(row)
            ref = row.get('reference') or ''
            if ref and db.execute("SELECT id FROM documents_vente WHERE reference=?", (ref,)).fetchone():
                nb_skip_dev += 1
                continue
            client_id_dst = client_id_map.get(row.get('client_id'))
            ttc = float(row.get('montant_ttc') or 0)
            tva_pct = float(row.get('tva') or 0)
            ht  = round(ttc / (1 + tva_pct / 100), 2) if tva_pct else ttc
            tva_mt = round(ttc - ht, 2)
            statut_map_d = {
                'accepte': 'converti', 'refuse': 'annule', 'expire': 'annule',
                'envoye': 'en_attente', 'brouillon': 'en_attente',
            }
            statut_dst = statut_map_d.get((row.get('statut') or '').lower(), 'en_attente')
            lignes_json = []
            try:
                import json as _json
                if row.get('lignes'):
                    lignes_json = _json.loads(row['lignes']) if isinstance(row['lignes'], str) else row['lignes']
            except Exception:
                lignes_json = []
            try:
                db.execute("""INSERT INTO documents_vente(
                               type_doc, reference, client_id, depot_id,
                               date_doc, statut,
                               total_ht, total_tva, total_ttc,
                               montant_paye, reste, mode_paiement, notes)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           ('devis', ref or None, client_id_dst, default_depot_id,
                            row.get('date_devis') or date.today().isoformat(),
                            statut_dst, ht, tva_mt, ttc, 0, ttc,
                            'especes', row.get('notes') or ''))
                new_dev_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                for i, l in enumerate(lignes_json or [], 1):
                    if not isinstance(l, dict):
                        continue
                    art_id_dst = article_id_map.get(l.get('stock_id') or l.get('article_id'))
                    qte  = float(l.get('quantite') or 1)
                    pu   = float(l.get('prix_unitaire') or l.get('prix_ht') or 0)
                    rem  = float(l.get('remise_pct') or 0)
                    tva_l = float(l.get('tva_pct') or tva_pct)
                    l_ht  = round(qte * pu * (1 - rem/100), 2)
                    l_ttc = l_ht + round(l_ht * tva_l / 100, 2)
                    try:
                        db.execute("""INSERT INTO lignes_vente(
                                       document_id, article_id, designation,
                                       quantite_unite, prix_ht, remise_pct, tva,
                                       total_ht, total_ttc, num_ligne)
                                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                   (new_dev_id, art_id_dst,
                                    l.get('designation') or '',
                                    qte, pu, rem, tva_l, l_ht, l_ttc, i))
                    except Exception:
                        pass
                nb_dev += 1
            except Exception:
                nb_skip_dev += 1
                rapport['erreurs'] += 1
        db.commit()
    _log('devis → documents_vente', nb_dev, nb_skip_dev)

    # ────────────────────────────────────────────────────────────────
    # 14. RÈGLEMENTS GESTIT → reglements + MAJ documents_vente
    # ────────────────────────────────────────────────────────────────
    nb_rgl = nb_skip_rgl = 0
    if 'reglements' in tables_src:
        for row in src.execute("SELECT * FROM reglements ORDER BY id"):
            row = _row(row)
            ref = row.get('reference') or ''
            if ref and db.execute("SELECT id FROM reglements WHERE reference=?", (ref,)).fetchone():
                nb_skip_rgl += 1
                continue
            client_id_dst = client_id_map.get(row.get('client_id'))
            # Résoudre source_id : vente ou facture atelier
            src_type = row.get('source_type') or 'vente'
            src_id_src = row.get('source_id')
            src_id_dst = None
            src_type_dst = 'facture'
            if src_type in ('vente', 'facture_vente'):
                src_id_dst = vente_id_map.get(src_id_src)
            elif src_type == 'facture':
                src_id_dst = facture_id_map.get(src_id_src)
            try:
                db.execute("""INSERT INTO reglements(
                               reference, source_type, source_id, client_id,
                               montant, mode_paiement, date_reglement, notes)
                               VALUES(?,?,?,?,?,?,?,?)""",
                           (ref or None,
                            src_type_dst,
                            src_id_dst,
                            client_id_dst,
                            float(row.get('montant') or 0),
                            row.get('mode_paiement') or 'especes',
                            row.get('date_reglement') or date.today().isoformat(),
                            row.get('notes') or ''))
                nb_rgl += 1
            except Exception:
                nb_skip_rgl += 1
                rapport['erreurs'] += 1
        db.commit()
    _log('reglements', nb_rgl, nb_skip_rgl)

    # ────────────────────────────────────────────────────────────────
    # 15. AVOIRS GESTIT → avoirs_clients
    # ────────────────────────────────────────────────────────────────
    nb_av = nb_skip_av = 0
    if 'avoirs' in tables_src:
        for row in src.execute("SELECT * FROM avoirs ORDER BY id"):
            row = _row(row)
            ref = row.get('reference') or ''
            if ref and db.execute("SELECT id FROM avoirs_clients WHERE reference=?", (ref,)).fetchone():
                nb_skip_av += 1
                continue
            client_id_dst = client_id_map.get(row.get('client_id'))
            if not client_id_dst:
                nb_skip_av += 1
                continue
            facture_id_dst = None
            if row.get('vente_id'):
                facture_id_dst = vente_id_map.get(row['vente_id']) or facture_id_map.get(row['vente_id'])
            ttc = float(row.get('montant_ttc') or 0)
            statut_map_a = {
                'valide': 'valide', 'rembourse': 'rembourse',
                'annule': 'annule', 'en_attente': 'en_attente',
            }
            statut_dst = statut_map_a.get((row.get('statut') or '').lower(), 'en_attente')
            try:
                db.execute("""INSERT INTO avoirs_clients(
                               reference, facture_id, client_id,
                               date_avoir, motif, type_avoir,
                               total_ht, total_tva, total_ttc,
                               statut, notes)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                           (ref or None,
                            facture_id_dst, client_id_dst,
                            row.get('date_avoir') or date.today().isoformat(),
                            row.get('motif') or '',
                            row.get('type_avoir') or 'retour',
                            0, 0, ttc,
                            statut_dst,
                            row.get('notes') or ''))
                nb_av += 1
            except Exception:
                nb_skip_av += 1
                rapport['erreurs'] += 1
        db.commit()
    _log('avoirs → avoirs_clients', nb_av, nb_skip_av)

    # ────────────────────────────────────────────────────────────────
    # 16. ÉCRITURES COMPTABLES GESTIT → ecritures_comptables
    # ────────────────────────────────────────────────────────────────
    nb_ec = nb_skip_ec = 0
    if 'ecritures' in tables_src:
        for row in src.execute("SELECT * FROM ecritures ORDER BY id"):
            row = _row(row)
            # Normaliser type_ecriture : recette ou depense uniquement
            type_src = (row.get('type_ecriture') or '').lower()
            if type_src in ('recette', 'credit', 'encaissement', 'vente', 'revenue'):
                type_dst = 'recette'
            else:
                type_dst = 'depense'
            libelle = row.get('libelle') or row.get('notes') or 'Écriture importée'
            categorie = row.get('categorie') or ('Ventes' if type_dst == 'recette' else 'Dépenses')
            # Doublon : même libellé + date + montant
            existing = db.execute("""SELECT id FROM ecritures_comptables
                WHERE libelle=? AND date_ecriture=? AND montant=?""",
                (libelle,
                 row.get('date_ecriture') or date.today().isoformat(),
                 float(row.get('montant') or 0))).fetchone()
            if existing:
                nb_skip_ec += 1
                continue
            # Résoudre source_id si lié à une vente
            src_name  = row.get('source') or None
            src_id_ec = row.get('source_id') or None
            if src_name == 'vente' and src_id_ec:
                src_id_ec = vente_id_map.get(src_id_ec, src_id_ec)
            elif src_name == 'facture' and src_id_ec:
                src_id_ec = facture_id_map.get(src_id_ec, src_id_ec)
            try:
                db.execute("""INSERT INTO ecritures_comptables(
                               type_ecriture, date_ecriture, categorie, libelle,
                               montant, mode_paiement, source, source_id, notes)
                               VALUES(?,?,?,?,?,?,?,?,?)""",
                           (type_dst,
                            row.get('date_ecriture') or date.today().isoformat(),
                            categorie, libelle,
                            float(row.get('montant') or 0),
                            row.get('mode_paiement') or 'especes',
                            src_name, src_id_ec,
                            row.get('notes') or ''))
                nb_ec += 1
            except Exception:
                nb_skip_ec += 1
                rapport['erreurs'] += 1
        db.commit()
    _log('ecritures_comptables', nb_ec, nb_skip_ec)

    src.close()
    return rapport


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    import threading, webbrowser, time

    # ── Lecture host/port depuis la base de données ─────────────────
    def _lire_config_reseau():
        """Lit server_host et server_port dans parametres sans contexte Flask."""
        host_val = '0.0.0.0'
        port_val = 1439
        if os.path.exists(DB_PATH):
            try:
                _db = sqlite3.connect(DB_PATH)
                rows = _db.execute(
                    "SELECT cle, valeur FROM parametres WHERE cle IN ('server_host','server_port')"
                ).fetchall()
                _db.close()
                cfg = {r[0]: r[1] for r in rows}
                h = cfg.get('server_host', '').strip()
                p = cfg.get('server_port', '').strip()
                if h:
                    host_val = h
                if p.isdigit() and 1024 <= int(p) <= 65535:
                    port_val = int(p)
            except Exception:
                pass  # Garde les valeurs par défaut si DB inaccessible
        return host_val, port_val

    HOST, PORT = _lire_config_reseau()

    # ── Bannière terminal ───────────────────────────────────────────
    print("")
    print("  ╔══════════════════════════════════════════════════════╗")
    print("  ║   🛒  DISTRIGEST — Gestion Commerciale               ║")
    print("  ║       STiNAUG TECHNOLOGIE · Abidjan, Côte d'Ivoire   ║")
    print("  ╠══════════════════════════════════════════════════════╣")
    print(f"  ║   ▶  Serveur    : http://{HOST}:{PORT}              ║")
    print("  ║   ▶  Login      : Admin / Admin123                   ║")
    print("  ║   ▶  Base       : data/distrigest.db                   ║")
    print("  ╠══════════════════════════════════════════════════════╣")
    print("  ║   Ctrl+C pour arrêter le serveur                     ║")
    print("  ╚══════════════════════════════════════════════════════╝")
    print("")

    # ── Initialisation DB ───────────────────────────────────────────
    try:
        init_db()
        print("  [OK] Base de données initialisée")
    except Exception as e:
        print(f"  [ERREUR] init_db : {e}")
        input("  Appuyez sur Entrée pour quitter...")
        raise

    # ── Ouverture navigateur ────────────────────────────────────────
    def ouvrir_navigateur():
        time.sleep(2.5)
        url_locale = f"http://localhost:{PORT}" if HOST in ('0.0.0.0', '127.0.0.1') else f"http://{HOST}:{PORT}"
        webbrowser.open(url_locale)

    threading.Thread(target=ouvrir_navigateur, daemon=True).start()
    print(f"  [OK] Navigateur s'ouvrira sur http://localhost:{PORT}")
    print("")

    # ── Démarrage serveur ───────────────────────────────────────────
    try:
        from waitress import serve
        print("  [OK] Moteur : Waitress (production)")
        print("  [..] Démarrage en cours...")
        print("")
        serve(app, host=HOST, port=PORT, threads=4)
    except ImportError:
        print("  [INFO] Waitress absent — mode Flask développement")
        print("")
        app.run(debug=False, port=PORT, host=HOST)
    except KeyboardInterrupt:
        print("")
        print("  [STOP] Serveur arrêté par l'utilisateur.")
        print("")
    except Exception as e:
        print(f"  [ERREUR] Serveur : {e}")
        logging.error("Erreur serveur : %s", e)
        input("  Appuyez sur Entrée pour quitter...")
        raise
