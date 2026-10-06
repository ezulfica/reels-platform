# Interface web locale

L’interface est une application FastAPI locale. Elle lit la même base SQLite que
la CLI et ne lance aucune étape de pipeline au chargement d’une page.

## Lancement

```bash
uv run reels web --port 8420
```

La CLI transmet le chemin de base choisi dans `REELS_DB_PATH`, puis Uvicorn
importe `interfaces.web.app:app`. En développement, ce module peut aussi être
lancé directement avec Uvicorn ; il utilise alors `db/reels.db`.

## Structure

- `src/interfaces/web/app.py` contient les routes FastAPI et adapte les faits du
  domaine aux templates. Il ne contient ni logique d’extraction ni SQL de
  mutation de pipeline.
- `src/interfaces/web/templates/` contient les pages Jinja rendues côté serveur.
  `base.html` porte le système visuel commun.
- `tests/test_web.py` couvre les routes, les médias locaux protégés et le suivi
  personnel.

## Données et limites

Les pages catalogue, recettes et reels consultent SQLite. Le suivi d’une entité
écrit explicitement dans `entity_personal` et son historique append-only. Les
médias sont servis seulement s’ils sont sous `MEDIA_ROOT`; le poster et le proxy
sont des dérivés, jamais le MP4 original remplacé.

Le chat conserve des sessions et messages dans des tables dédiées (`chat_session`
et `chat_message`), séparées des faits extraits. À chaque tour, il interroge
d’abord SQLite puis transmet un contexte borné au LLM configuré.

## Planification et profil LLM

`/settings` conserve le planning et le profil d’inférence dans
`~/.config/reels-platform/runtime.yaml` (non versionné, permissions utilisateur).
Lors de l’application, l’interface écrit des overrides systemd utilisateur pour
`reels-weekly.service` et `reels-weekly.timer`, puis recharge systemd. Elle ne
lit ni n’écrit de clé API, cookie Instagram ou session Codex. Les profils
proposés choisissent la connexion, le modèle et l’effort de raisonnement par des
variables de backend non sensibles. Ils s’appliquent au prochain run hebdomadaire
et aux nouvelles requêtes locales du chat ou de la pipeline manuelle ; la
connexion correspondante doit déjà fonctionner sur la machine.

Les profils vivent dans le catalogue versionné `config/llm-profiles.yaml`.
L’interface sélectionne un profil ; les définitions restent relisibles et
révisables comme du code. Un catalogue local facultatif dans
`~/.config/reels-platform/llm-profiles.local.yaml` peut ajouter des profils de
machine non sensibles sans modifier le fichier projet.
