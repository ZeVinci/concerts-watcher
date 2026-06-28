#!/usr/bin/env python3
"""
Surveillance de concerts en Ile-de-France pour une liste d'artistes,
via l'API publique Bandsintown.

Principe :
  1. pour chaque artiste -> 1 requete /artists/{name}/events (tous ses concerts)
  2. filtrage cote script sur l'Ile-de-France (boite geo + whitelist salles)
  3. diff contre seen.json (les events deja signales)
  4. alerte sur les nouveautes uniquement, puis mise a jour de l'etat

Le premier passage (seen.json absent) est un "seed" : on enregistre l'etat
sans rien notifier, pour ne pas etre noye par tout le catalogue existant.

Variables d'environnement (cf. README.md) :
  BANDSINTOWN_APP_ID   requis
  NOTIFY_METHOD        ntfy | email | none      (defaut: ntfy)
  NTFY_URL             ex: https://ntfy.sh/<topic-secret>
  SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASS EMAIL_TO   (si NOTIFY_METHOD=email)
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import sys
import time
import unicodedata
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
ARTISTS_FILE = ROOT / "artists.json"
VENUES_FILE = ROOT / "venues.json"        # optionnel (whitelist salles)
STATE_FILE = ROOT / "seen.json"

API_URL = "https://rest.bandsintown.com/artists/{name}/events"
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN = 0.6        # politesse entre requetes (sec)
MAX_RETRIES = 2
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}

# Boite englobante Ile-de-France (large, volontairement permissive)
IDF_LAT_MIN, IDF_LAT_MAX = 48.10, 49.25
IDF_LON_MIN, IDF_LON_MAX = 1.40, 3.60

# Villes IDF frequentes pour les concerts (filet de securite si lat/lon manque)
IDF_CITIES = {
    "paris", "boulogne-billancourt", "saint-denis", "montreuil", "nanterre",
    "aubervilliers", "pantin", "malakoff", "issy-les-moulineaux", "bobigny",
    "creteil", "vincennes", "la defense", "puteaux", "ivry-sur-seine",
    "saint-ouen", "noisy-le-grand", "rungis", "le bourget",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("concerts")


# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #
def normalize(text: str) -> str:
    """minuscule, sans accents, espaces normalises."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.lower().split())


def encode_artist(name: str) -> str:
    """
    Encode le nom pour l'URL Bandsintown.
    Cas special documente par BIT : un '/' litteral dans un nom doit devenir
    '%252F' (double-encode). Aucun artiste de la liste actuelle n'en contient,
    mais on gere le cas pour eviter une surprise future.
    """
    name = name.replace("/", "%252F")
    return quote(name, safe="%")


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Lecture de %s impossible (%s) -> valeur par defaut", path.name, exc)
        return default


# --------------------------------------------------------------------------- #
# Filtrage Ile-de-France
# --------------------------------------------------------------------------- #
def load_venue_keywords() -> list[str]:
    """Extrait les sous-chaines normalisees de venues.json (whitelist optionnelle)."""
    data = load_json(VENUES_FILE, {})
    keywords = []
    for v in data.get("venues", []):
        kw = v.get("match") or v.get("name")
        if kw:
            keywords.append(normalize(kw))
    return keywords


def is_idf(venue: dict, venue_keywords: list[str]) -> bool:
    """Vrai si la salle est (probablement) en Ile-de-France."""
    country = normalize(venue.get("country", ""))
    if country not in {"france", "fr"}:
        return False

    # 1) boite geographique (filtre principal)
    try:
        lat = float(venue.get("latitude"))
        lon = float(venue.get("longitude"))
        if IDF_LAT_MIN <= lat <= IDF_LAT_MAX and IDF_LON_MIN <= lon <= IDF_LON_MAX:
            return True
    except (TypeError, ValueError):
        pass  # lat/lon absents ou invalides -> on tente les filets suivants

    # 2) ville connue
    if normalize(venue.get("city", "")) in IDF_CITIES:
        return True

    # 3) whitelist de salles (bonus, sur le nom de la salle)
    vname = normalize(venue.get("name", ""))
    return any(kw and kw in vname for kw in venue_keywords)


# --------------------------------------------------------------------------- #
# Appel API
# --------------------------------------------------------------------------- #
def fetch_events(artist_name: str, app_id: str) -> list[dict]:
    """Renvoie la liste des events a venir d'un artiste, ou [] en cas de souci."""
    url = API_URL.format(name=encode_artist(artist_name))
    params = {"app_id": app_id, "date": "upcoming"}

    for attempt in range(1, MAX_RETRIES + 2):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code == 404:
                log.info("  %-32s introuvable (404)", artist_name)
                return []
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):           # ex: {"errorMessage": "..."}
                log.info("  %-32s reponse non-liste (%s)", artist_name,
                         data.get("errorMessage", "?"))
                return []
            return data if isinstance(data, list) else []
        except (requests.RequestException, ValueError) as exc:
            if attempt <= MAX_RETRIES:
                time.sleep(1.5 * attempt)
                continue
            log.warning("  %-32s echec apres %d essais (%s)",
                        artist_name, attempt, exc)
            return []
    return []


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def format_event(ev: dict) -> str:
    venue = ev.get("venue", {})
    when = ev.get("datetime", "")[:10]
    salle = venue.get("name", "?")
    ville = venue.get("city", "")
    artist = ev.get("_artist", "?")
    url = ev.get("url", "")
    line = f"- {artist} — {salle}, {ville} — {when}"
    return f"{line}\n  {url}" if url else line


def build_message(new_events: list[dict]) -> tuple[str, str]:
    n = len(new_events)
    title = f"{n} nouveau{'x' if n > 1 else ''} concert{'s' if n > 1 else ''} en IDF"
    body_lines = ["Nouvelles annonces detectees depuis le dernier scan :", ""]
    for ev in sorted(new_events, key=lambda e: e.get("datetime", "")):
        body_lines.append(format_event(ev))
    return title, "\n".join(body_lines)


def notify(new_events: list[dict]) -> None:
    method = (os.environ.get("NOTIFY_METHOD") or "ntfy").lower()
    if not new_events or method == "none":
        return
    title, body = build_message(new_events)

    if method == "ntfy":
        ntfy_url = os.environ.get("NTFY_URL")
        if not ntfy_url:
            log.error("NOTIFY_METHOD=ntfy mais NTFY_URL absent.")
            return
        try:
            requests.post(
                ntfy_url,
                data=body.encode("utf-8"),
                headers={"Title": title, "Tags": "musical_note", "Priority": "default"},
                timeout=REQUEST_TIMEOUT,
            ).raise_for_status()
            log.info("Notification ntfy envoyee.")
        except requests.RequestException as exc:
            log.error("Echec ntfy : %s", exc)

    elif method == "email":
        host = os.environ.get("SMTP_HOST")
        port = int(os.environ.get("SMTP_PORT", "587"))
        user = os.environ.get("SMTP_USER")
        passwd = os.environ.get("SMTP_PASS")
        to = os.environ.get("EMAIL_TO", user)
        if not all([host, user, passwd, to]):
            log.error("NOTIFY_METHOD=email mais configuration SMTP incomplete.")
            return
        msg = MIMEText(body, _charset="utf-8")
        msg["Subject"] = title
        msg["From"] = user
        msg["To"] = to
        try:
            with smtplib.SMTP(host, port, timeout=REQUEST_TIMEOUT) as srv:
                srv.starttls()
                srv.login(user, passwd)
                srv.send_message(msg)
            log.info("Email envoye a %s.", to)
        except (smtplib.SMTPException, OSError) as exc:
            log.error("Echec email : %s", exc)
    else:
        log.error("NOTIFY_METHOD inconnu : %s", method)


# --------------------------------------------------------------------------- #
# Programme principal
# --------------------------------------------------------------------------- #
def main() -> int:
    app_id = os.environ.get("BANDSINTOWN_APP_ID")
    if not app_id:
        log.error("BANDSINTOWN_APP_ID manquant. Arret.")
        return 1

    artists = load_json(ARTISTS_FILE, {}).get("artists", [])
    if not artists:
        log.error("Aucun artiste dans %s. Arret.", ARTISTS_FILE.name)
        return 1

    venue_keywords = load_venue_keywords()
    state = load_json(STATE_FILE, None)
    first_run = state is None
    seen_ids = set(state.get("seen_ids", [])) if state else set()

    log.info("Scan de %d artistes%s.", len(artists),
             " (premier passage : seed, pas d'alerte)" if first_run else "")

    idf_events: list[dict] = []
    for entry in artists:
        name = entry.get("name", "").strip()
        if not name:
            continue
        events = fetch_events(name, app_id)
        kept = 0
        for ev in events:
            if is_idf(ev.get("venue", {}), venue_keywords):
                ev["_artist"] = name
                ev["_pole"] = entry.get("pole", "")
                idf_events.append(ev)
                kept += 1
        if kept:
            log.info("  %-32s %d concert(s) IDF", name, kept)
        time.sleep(SLEEP_BETWEEN)

    # Diff
    current_ids = {str(ev.get("id")) for ev in idf_events}
    new_events = [ev for ev in idf_events if str(ev.get("id")) not in seen_ids]

    log.info("Bilan : %d concerts IDF au total, %d nouveau(x).",
             len(idf_events), len(new_events))

    if first_run:
        log.info("Seed initial : enregistrement sans alerte.")
    elif new_events:
        for ev in new_events:
            log.info("  NOUVEAU : %s", format_event(ev).splitlines()[0])
        notify(new_events)

    # Mise a jour de l'etat : union (on ne perd pas un id qui disparait
    # temporairement du flux, ce qui eviterait une re-alerte au retour)
    updated = seen_ids | current_ids
    STATE_FILE.write_text(
        json.dumps(
            {
                "seen_ids": sorted(updated),
                "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "last_count": len(idf_events),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log.info("Etat ecrit (%d ids memorises).", len(updated))
    return 0


if __name__ == "__main__":
    sys.exit(main())
