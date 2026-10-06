# Local MCP for the Discord agent

The MCP server exposes the Reels Platform database to an external agent in
read-only mode. It cannot start pipeline stages, write SQLite, or access local
media, cookies or secrets.

## Run

```bash
cd /path/to/reels-platform
uv run --extra mcp reels-mcp
```

The transport is `stdio`: the MCP host starts this local process and reads its
stdout. Set `REELS_DB_PATH` in the process environment to choose another
database.

## Tools

- `database_summary`
- `search_entities`
- `get_entity`
- `get_reel`
- `search_recipes`
- `search_repertoire`

Configure the Discord host with the `uv run --extra mcp reels-mcp` command and
repository working directory. The agent must search before recommending and cite
the returned source reels.
