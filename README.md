# Surveillance concerts IDF

Scan quotidien (via GitHub Actions) des annonces de concerts en Île-de-France
pour une liste d'artistes, avec alerte sur les **nouveautés depuis le dernier passage**.

## Comment ça marche

- `artists.json` — tes artistes à surveiller (champ `name` passé à l'API Bandsintown).
- `venues.json` — whitelist de salles, **optionnelle**, en bonus du filtre géographique.
- `monitor.py` — pour chaque artiste, une requête `/artists/{name}/events` (tous ses
  concerts), filtrage Île-de-France, diff contre `seen.json`, puis notification.
- `seen.json` — fichier d'état (créé au premier run, recommité par le workflow).
- `.github/workflows/concerts.yml` — cron quotidien + commit de l'état.

Le filtre IDF combine trois critères : boîte géographique (lat/lon), liste de villes
connues, et whitelist de salles — pour rester robuste face aux données parfois sales
de Bandsintown (coordonnées manquantes, nom de salle écrasé par le titre de l'event).

## Mise en place

1. **Crée un dépôt GitHub privé** et pousse ces fichiers à la racine.

2. **Obtiens un `app_id` Bandsintown.** L'endpoint répond avec n'importe quelle chaîne,
   mais les CGU réservent en principe l'API aux artistes/partenaires et demandent un
   `app_id` personnel obtenu auprès d'eux. Usage perso à faible volume = zone grise,
   à toi d'apprécier.

3. **Choisis ton canal d'alerte :**
   - **ntfy (recommandé, le plus simple)** : installe l'appli ntfy (Android/iOS),
     abonne-toi à un topic au nom secret (ex. `concerts-idf-x7k2`), puis règle le
     secret `NTFY_URL = https://ntfy.sh/concerts-idf-x7k2`. Aucun compte requis.
   - **Email** : `NOTIFY_METHOD = email` + secrets `SMTP_HOST`, `SMTP_PORT` (587),
     `SMTP_USER`, `SMTP_PASS` (mot de passe d'application si Gmail), `EMAIL_TO`.

4. **Ajoute les secrets** dans *Settings → Secrets and variables → Actions* :
   `BANDSINTOWN_APP_ID` (requis), puis `NTFY_URL` **ou** les `SMTP_*`.
   (`NOTIFY_METHOD` vaut `ntfy` par défaut ; inutile de le définir pour ntfy.)

5. **Premier lancement = seed.** Va dans l'onglet *Actions*, lance le workflow
   manuellement (*Run workflow*). Comme `seen.json` n'existe pas encore, ce passage
   enregistre l'état **sans alerter** : tu ne reçois donc pas tout le catalogue d'un
   coup. Les vraies alertes commencent au passage suivant.

6. **Règle la fréquence** dans `concerts.yml` (ligne `cron`). Quotidien par défaut ;
   `0 7 * * 1,3,5` pour lundi/mercredi/vendredi.

## Tester en local

```bash
pip install -r requirements.txt
export BANDSINTOWN_APP_ID="ton-app-id"
export NOTIFY_METHOD="none"        # n'envoie rien, log seulement
rm -f seen.json                    # force un seed
python monitor.py                  # 1er run : seed
python monitor.py                  # 2e run : diff (vide si rien de neuf)
```

## Maintenance

- **Ajouter des artistes** : édite `artists.json`. Vérifie les entrées marquées
  `"verify": true` (noms génériques/ambigus) en ouvrant leur page Bandsintown.
- **Compléter la liste** : recharge l'export Deezer pour passer du cœur (~68) au
  top-100 complet.
- **Élargir/restreindre la zone** : ajuste la boîte `IDF_*` ou `IDF_CITIES` dans
  `monitor.py`.
