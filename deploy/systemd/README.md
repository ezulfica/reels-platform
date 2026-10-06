# Services utilisateur

Installer les unités locales :

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/reels-*.service deploy/systemd/reels-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now reels-web.service reels-weekly.timer
```

Le web écoute sur `http://127.0.0.1:8420`. Le timer lance le dimanche à 10 h,
et rattrape un déclenchement manqué au prochain démarrage grâce à `Persistent=true`.

Le serveur MCP n'est pas une unité systemd : l'agent Discord l'exécute en
sous-processus stdio avec `uv run --extra mcp reels-mcp`.
