# MCP local pour l’agent Discord

Le serveur MCP expose la base Reels Platform à un agent externe en lecture seule.
Il ne lance aucune étape de pipeline et ne peut ni écrire dans SQLite, ni accéder
aux cookies, médias locaux ou secrets.

## Lancement

```bash
cd /path/to/reels-platform
uv run --extra mcp reels-mcp
```

Le transport est `stdio` : l’hôte MCP lance ce processus localement et lit son
stdout. Ne pas y écrire de logs ou de texte hors protocole.

Pour choisir une autre base, transmettre `REELS_DB_PATH` dans l’environnement du
processus MCP.

## Outils

- `database_summary`
- `search_entities`
- `get_entity`
- `get_reel`
- `search_recipes`
- `search_repertoire`

Enregistrer ce serveur dans l’hôte Discord avec la commande `uv`, les arguments
`run --extra mcp reels-mcp`, et le répertoire de travail du dépôt. L’agent doit
chercher avant de recommander et citer les reels source retournés par les outils.
