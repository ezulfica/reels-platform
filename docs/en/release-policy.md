# Release policy

Reels Platform uses semantic versioning and annotated Git tags in the form
`vMAJOR.MINOR.PATCH`. The tag must match the `project.version` in
`pyproject.toml`: for example, version `0.2.1` is released as `v0.2.1`.

- **Patch** fixes behaviour without changing the supported contracts.
- **Minor** adds backward-compatible capabilities.
- **Major** changes a persisted-data, CLI, MCP, or configuration contract that
  needs migration or a deliberate operator action.

Before tagging, work on a feature branch, merge into `main`, and run:

```bash
uv lock --check
uv run ruff check --select E9,F63,F7,F82 src tests
uv run pytest -q
```

Then update `project.version`, commit it, and create the annotated tag from
`main`:

```bash
git tag -a vX.Y.Z -m "Reels Platform vX.Y.Z"
```

Pushing a matching tag runs CI, builds the wheel and source distribution, and
creates the GitHub release with generated notes. No data migration, corpus
replay, or external publication is triggered by a release tag.
