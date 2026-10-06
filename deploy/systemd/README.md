# Services utilisateur

Depuis la racine du dépôt, installer ou actualiser les unités locales :

```bash
./scripts/install-systemd.sh
```

L’installateur crée `~/.config/reels-platform/systemd.env` avec le chemin du
checkout courant et de sa base SQLite. Ce fichier ne contient aucun secret et
est régénéré à chaque installation : il permet de déplacer le checkout sans
modifier les unités versionnées.

Le web écoute sur `http://127.0.0.1:8420`. Le timer lance le dimanche à 10 h,
et rattrape un déclenchement manqué au prochain démarrage grâce à `Persistent=true`.

Le serveur MCP n'est pas une unité systemd : l'agent Discord l'exécute en
sous-processus stdio avec `uv run --extra mcp reels-mcp`.
