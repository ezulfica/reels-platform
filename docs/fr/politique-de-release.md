# Politique de release

Reels Platform utilise le versionnement sémantique et des tags Git annotés au
format `vMAJOR.MINOR.PATCH`. Le tag doit correspondre à `project.version` dans
`pyproject.toml` : par exemple, la version `0.2.1` se publie avec `v0.2.1`.

- **Patch** corrige un comportement sans modifier les contrats supportés.
- **Minor** ajoute une capacité rétrocompatible.
- **Major** modifie un contrat de données persistées, de CLI, de MCP ou de
  configuration qui demande une migration ou une action explicite.

Avant de taguer, travailler sur une branche, fusionner dans `main`, puis lancer :

```bash
uv lock --check
uv run ruff check --select E9,F63,F7,F82 src tests
uv run pytest -q
```

Mettre ensuite à jour `project.version`, le committer et créer le tag annoté
depuis `main` :

```bash
git tag -a vX.Y.Z -m "Reels Platform vX.Y.Z"
```

Le push d’un tag correspondant lance la CI, produit les distributions et crée la
release GitHub avec ses notes générées. Un tag ne déclenche ni migration de
données, ni replay du corpus, ni publication externe de contenu.
