# SmartCam

<center>
  
![image](https://github.com/user-attachments/assets/b1091edf-5e2d-484a-8554-55cde0cacbbc)

</center>


**Version**: 1.0

---

## Overview

SmartCam GUI is a Linux (V4L2) video surveillance application featuring:

* **Motion detection** using MOG2 background subtraction with live indicator.
* **Pre/post buffering** configurable to record seconds before/after events.
* **Segmented recording**: separate `.avi` files for each motion event.
* **Deferred compression** (to MP4) running in background.
* **Optional RTSP streaming** to remote indexer via FFmpeg.
* **Automated email delivery** (real-time or on exit) with ZIP attachments.
* **PyQt5 GUI**  clean and responsive.
* **Headless mode** (CLI-only) for minimal systems.
* **Robust error handling** (camera access, permissions, SMTP failures).
* **Configurable logging** (`--log` flag) at `DEBUG` or `WARNING` levels.

## Présentation

SmartCam GUI est une application de surveillance vidéo pour Linux (V4L2) offrant :

* **Détection de mouvement** par soustraction de fond (MOG2) avec indicateur visuel.
* **Tampon pré/post** configurable pour capturer quelques secondes avant et après chaque détection.
* **Enregistrement segmenté** : fichiers `.avi` par événement.
* **Compression différée** (transcodage en MP4) en arrière-plan.
* **Flux RTSP** optionnel vers un indexeur distant via FFmpeg.
* **Envoi d’emails** automatisé (mode **realtime** ou **on\_exit**) avec attachement ZIP.
* **Interface PyQt5** claire et fluide.
* **Mode headless** (sans GUI), accessible en CLI.
* **Gestion robuste des erreurs** (caméra inaccessible, droits, SMTP, etc.).
* **Journalisation** configurable (`--log`) en niveaux `DEBUG` / `WARNING`.

---
---

## Dépendances / Dependencies

* Python >= 3.8
* PyQt5
* opencv-python-headless
* numpy
* python-dotenv
* FFmpeg (installé et accessible via `$PATH`)

```bash
pip install PyQt5 opencv-python-headless numpy python-dotenv
# sudo apt install ffmpeg
```

---

## Installation

```bash
# Clone le dépôt
git clone https://github.com/username/smartcam.git
cd smartcam
# Crée et active un environnement virtuel
python3 -m venv venv
source venv/bin/activate
# Installe les dépendances Python
pip install -r requirements.txt
```

---

## Fichier de configuration (`smartcam_config.json`)

Le fichier JSON est généré automatiquement à la première exécution. Toutes les options y sont stockées :

```json
{
  "device": "/dev/video0",
  "fps": 20,
  "area": 1500,
  "hits": 5,
  "pre_s": 3,
  "post_s": 3,
  "out_dir": "/home/user/Videos",
  "prefix": "segment_",
  "mail": { ... },
  "send_mode": "on_exit",
  "rtsp": { ... },
  "compress_after_min": 30,
  "preview_raw": true,
  "preview_proc": true
}
```

Modifiez ce fichier ou utilisez l’onglet **Configuration** dans la GUI pour adapter les paramètres.

---

## Usage

### Mode GUI

```bash
# Lancement avec GUI (thème Telegram)
python smart_cam.py --gui
# Activer les logs DEBUG
python smart_cam.py --gui --log
```

### Mode headless (sans interface)

```bash
# Headless, logs WARNING seul
guard python smart_cam.py
# Headless, logs DEBUG détaillés
guard python smart_cam.py --log
```

Une fois la capture lancée :

* GUI : cliquez sur **Start** pour démarrer/arrêter.
* Headless : `Ctrl+C` pour interrompre proprement.

---

## Structure du projet

```
smartcam/
├── smart_cam.py    # Script principal
├── smartcam_config.json
├── requirements.txt
└── README.md
```

---

## Contribution

Les contributions sont les bienvenues ! Merci de :

1. **Forker** le dépôt.
2. Créer une **branche** fonctionnelle.
3. Soumettre une **Pull Request** détaillant vos changements.

---

## Licence

MIT License © 2025 - Paul BERRA

---

