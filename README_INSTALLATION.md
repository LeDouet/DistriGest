# DISTRIGEST — Guide d'installation multi-plateforme

**STiNAUG TECHNOLOGIE · Abidjan, Côte d'Ivoire**
Version 1.0

---

## Vue d'ensemble

DISTRIGEST est une application web Flask qui fonctionne sur toutes les plateformes.
Le modèle de déploiement varie selon la plateforme cible :

| Plateforme        | Mode de distribution       | Fichier livrable                    |
|-------------------|----------------------------|-------------------------------------|
| Windows           | Installeur `.exe`          | `DISTRIGEST_v1.0_Setup.exe`         |
| macOS             | Bundle `.dmg` ou `.app`    | `DISTRIGEST_v1.0_macOS.dmg`         |
| Android / Tablette | PWA (navigateur Chrome)   | Aucun — accès via IP réseau         |
| iPad / iPhone     | PWA (navigateur Safari)    | Aucun — accès via IP réseau         |

---

## 🪟 Windows

### Prérequis
- Windows 10 / 11 (64 bits)
- Python 3.10+ (si build depuis les sources)

### Installation pour l'utilisateur final
1. Double-cliquer `DISTRIGEST_v1.0_Setup.exe`
2. Suivre l'assistant d'installation
3. Lancer via l'icône Bureau ou le menu Démarrer

### Build depuis les sources
```bat
build.bat          ← Compile l'exécutable
build_setup.bat    ← Crée l'installeur .exe
```

**Prérequis du build :** Python 3.10+, Inno Setup 6

---

## 🍎 macOS

### Prérequis
- macOS 11 Big Sur ou supérieur
- Python 3.10+ installé (`brew install python` ou python.org)
- Homebrew recommandé

### Installation pour l'utilisateur final
1. Ouvrir `DISTRIGEST_v1.0_macOS.dmg`
2. Glisser `DISTRIGEST.app` dans le dossier Applications
3. **Première ouverture :** clic droit → Ouvrir → "Ouvrir quand même"
   _(macOS bloque les apps non signées par défaut — comportement normal)_
4. L'app ouvre automatiquement le navigateur sur `http://localhost:1439`

> **Astuce :** Si le message Gatekeeper bloque toujours l'app :
> Préférences Système → Sécurité et confidentialité → cliquer "Autoriser quand même"

### Build depuis les sources _(sur un Mac uniquement)_
```bash
bash build_mac.sh
```

Le script :
- Détecte automatiquement l'architecture (Apple Silicon M1/M2/M3 ou Intel)
- Compile avec PyInstaller
- Crée le `.dmg` dans `installer/`

**Prérequis du build :**
```bash
pip3 install pyinstaller waitress flask reportlab
brew install create-dmg   # optionnel, pour le DMG graphique
```

---

## 📱 Android & Tablettes Android

DISTRIGEST fonctionne sur Android comme **PWA (Progressive Web App)** :
l'utilisateur accède à l'interface via Chrome et l'installe comme une vraie app.

### Étape 1 — Démarrer le serveur sur le PC

Sur le PC Windows ou Mac qui héberge DISTRIGEST, noter l'adresse IP locale :
- Windows : `ipconfig` dans cmd → chercher "Adresse IPv4" → ex. `192.168.1.10`
- Mac : Préférences Système → Réseau → ex. `192.168.1.10`

Démarrer DISTRIGEST normalement.

### Étape 2 — Connexion depuis Android

1. S'assurer que le téléphone/tablette est sur le **même réseau Wi-Fi**
2. Ouvrir **Chrome** sur Android
3. Saisir : `http://192.168.1.10:1439` _(remplacer par votre IP)_
4. Se connecter normalement

### Étape 3 — Installer comme app native

Dans Chrome, appuyer sur le menu ⋮ (trois points) → **"Ajouter à l'écran d'accueil"**

DISTRIGEST apparaît comme une vraie application :
- Icône sur l'écran d'accueil
- Ouverture en plein écran (sans barre d'adresse)
- Fonctionne en mode paysage/portrait

### Remarque réseau

Pour un accès permanent depuis Android (sans PC allumé), DISTRIGEST doit être
déployé sur un serveur ou NAS accessible en permanence. Contactez STiNAUG TECHNOLOGIE
pour une offre d'hébergement ou de déploiement serveur local.

---

## 🍎 iPad / iPhone (iOS)

Même principe que Android, via **Safari** (et non Chrome sur iOS).

### Procédure

1. Ouvrir **Safari** sur l'iPad ou iPhone
2. Saisir `http://192.168.1.10:1439` (IP du serveur DISTRIGEST)
3. Se connecter
4. Appuyer sur l'icône de partage ↑ → **"Sur l'écran d'accueil"**

> **Note iOS :** Apple impose Safari pour les PWA sur iOS (Chrome iOS ne permet
> pas l'installation). Les PWA iOS ont quelques limitations mineures par rapport
> à Android (pas de notifications push, etc.) mais l'utilisation quotidienne
> de DISTRIGEST n'est pas affectée.

---

## 🌐 Déploiement réseau (multi-poste)

Pour permettre à plusieurs utilisateurs (sur PC, tablette, téléphone) d'utiliser
DISTRIGEST simultanément :

1. Démarrer DISTRIGEST sur **un seul PC serveur** (ou NAS)
2. Chaque poste/tablette/téléphone accède via le navigateur : `http://IP_SERVEUR:1439`
3. Le serveur Waitress intégré supporte plusieurs connexions simultanées (4 threads)

**Configuration du pare-feu Windows (si nécessaire) :**
```
Pare-feu Windows → Règles de trafic entrant → Nouvelle règle
→ Port → TCP → 1439 → Autoriser → Tous les profils
Nom : DISTRIGEST
```

---

## ❓ Dépannage

| Symptôme | Solution |
|----------|----------|
| "Connexion refusée" depuis Android/iPad | Vérifier que PC et mobile sont sur le même Wi-Fi. Vérifier le pare-feu Windows. |
| macOS bloque l'app | Clic droit → Ouvrir → Ouvrir quand même |
| Le port 1439 est déjà utilisé | Fermer l'autre instance DISTRIGEST ou redémarrer le PC |
| La PWA ne s'installe pas | Sur Android : utiliser Chrome. Sur iOS : utiliser Safari. |
| Page blanche après installation PWA | Le serveur DISTRIGEST doit être démarré et accessible |

---

## 📞 Support

**STiNAUG TECHNOLOGIE**
Abidjan, Côte d'Ivoire
