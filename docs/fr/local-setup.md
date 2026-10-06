# Installation locale et exploitation

Reels Platform est une application locale. SQLite, les médias téléchargés, les
cookies, les credentials de modèles et le feedback personnel restent sur la
machine. L’interface web lit la base ; le chargement d’une page ne lance jamais
la pipeline.

## 1. Installer et consulter une bibliothèque existante

Prérequis : Python 3.11–3.12, `uv`, `ffmpeg`, `yt-dlp` et, pour l’enrichissement,
les dépendances ASR et OCR installées par `uv sync`.

```bash
make dev
make web
```

Ouvrir <http://127.0.0.1:8420>. Cette étape ne nécessite ni cookie Instagram ni
connexion LLM lorsqu’une base locale existe déjà. `make dev` synchronise les
dépendances de développement et les interfaces facultatives, crée `.env` depuis
le modèle seulement s’il est absent, puis active les hooks Git. `make web`
lance l’interface locale et `make watch` ajoute le rechargement automatique ;
aucune de ces commandes ne déclenche la pipeline. `PORT` et `DB` restent
configurables : `make web PORT=8421 DB=/chemin/vers/reels.db`. Utiliser
`make doctor` pour inspecter les prérequis facultatifs.

## 2. Configurer les secrets et l’inférence

`make dev`, `make web` et `make watch` créent le fichier local non suivi depuis
le modèle, avec les permissions utilisateur, seulement s’il n’existe pas.
Ajouter une valeur uniquement si cette machine capture des médias Instagram
sauvegardés.

`.env.example` ne contient volontairement que `IG_SESSIONID`. La configuration
par défaut utilise la CLI Codex authentifiée, Luna low et l’ASR CUDA : parcourir
la base et utiliser l’inférence par défaut ne demande aucun réglage de modèle
dans `.env`. Les variables du shell sont prioritaires. Ne jamais versionner
`.env` ni placer un cookie Instagram, une clé API ou une session Codex dans une
unité systemd ou l’interface web.

Choisir un backend avec `REELS_LLM_BACKEND` :

| Backend | Préparation locale requise | Réglages principaux |
|---|---|---|
| `codex` | CLI `codex` authentifiée | `REELS_MODEL_CODEX`, `REELS_CODEX_REASONING` |
| `hermes` | Exécutable et connexion Hermes fonctionnels | `REELS_HERMES_EXECUTABLE`, `REELS_HERMES_REASONING` |
| `api` | Endpoint compatible OpenAI et clé API | `REELS_API_BASE_URL`, `REELS_API_KEY`, `REELS_MODEL_API` |
| `ollama` | Ollama lancé avec le modèle choisi | `REELS_MODEL_EXTRACT` |

Les profils disponibles sont versionnés dans
[`config/llm-profiles.yaml`](../../config/llm-profiles.yaml). L’interface web
sélectionne un profil mais ne modifie jamais ce fichier projet. Un catalogue de
profils propre à la machine peut être ajouté dans
`~/.config/reels-platform/llm-profiles.local.yaml` ; il reprend le même schéma,
ne peut pas écraser un profil projet et n’est pas versionné. Le profil actif et
le planning sont conservés dans `~/.config/reels-platform/runtime.yaml` avec des
permissions utilisateur.

Pour la capture, ajouter `IG_SESSIONID` depuis une session Instagram déjà ouverte
dans le navigateur. Aucun mot de passe n’est utilisé. Ajouter d’autres cookies
seulement si Instagram refuse la session minimale et que le navigateur les donne.

L’ASR CUDA est le défaut du poste. Régler `REELS_ASR_DEVICE=cpu` seulement sur
une machine sans runtime NVIDIA : l’ASR CPU est nettement plus lent.

## 3. Lancer la pipeline volontairement

Chaque étape est idempotente et reprend ses éléments inachevés. Cela ne signifie
pas qu’une commande est petite : le backlog en attente peut être conséquent.
Inspecter l’état avant de lancer un traitement.

```bash
uv run reels status

# Scan incrémental des sauvegardes uniquement.
uv run reels sync

# Reprise précise d’un téléchargement ou d’une extraction en échec.
uv run reels download --shortcode SHORTCODE
uv run reels extract --shortcode SHORTCODE
```

`reels pipeline` enchaîne scan incrémental, téléchargement, ASR, OCR,
extraction, vérification et catalogue. `reels weekly-run` est son équivalent
planifié. Ne pas les utiliser pour seulement tester des credentials lorsqu’un
backlog est important ; employer une base temporaire ou une commande bornée.
`--force` ré-extrait des éléments déjà terminés et doit rester limité à un
échantillon explicite d’évaluation.

```bash
# Exploitation locale régulière après confirmation de la configuration.
uv run reels weekly-run

# Consultation et recherche sans écrire de faits de pipeline.
uv run reels query "restaurant japonais" --city Paris
uv run reels recipes --cuisine japanese
```

## 4. Services locaux

Le web écoute uniquement sur `127.0.0.1:8420`. La page de configuration conserve
le planning, la connexion, le modèle et l’effort de raisonnement dans
`~/.config/reels-platform/runtime.yaml` ; elle ne conserve aucun secret. Le
profil choisi s’applique aux nouvelles requêtes locales du chat et de la pipeline
manuelle, ainsi qu’au prochain run hebdomadaire. Installer les services
utilisateur une fois la configuration locale prête :

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/reels-*.service deploy/systemd/reels-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now reels-web.service reels-weekly.timer
```

Le MCP n’est pas un service réseau. L’agent externe le lance comme sous-processus
stdio en lecture seule :

```bash
uv run --extra mcp reels-mcp
```

## 5. Vérifier les changements

```bash
uv run pytest -q
uv run reels regression
git diff --check
```

La régression évalue des résultats d’extraction matérialisés. Elle n’effectue pas
d’appel LLM réel par elle-même. La qualité d’un modèle relève d’une évaluation
séparée sur jeu annoté gelé, hors CI de pull request.
