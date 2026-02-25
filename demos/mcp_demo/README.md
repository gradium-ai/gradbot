# MCP Voice Demo

A voice AI assistant that connects to [MCP](https://modelcontextprotocol.io/) servers and exposes their tools through conversation. Ask it to manage files, remember things, or anything else your MCP servers can do.

By default it connects to two MCP servers:
- **Filesystem** — read, write, search, and manage files in `/tmp/mcp-workspace`
- **Memory** — a persistent knowledge graph (remember facts, recall them later)

Swap the MCP servers in `config.yaml` and you get a completely different assistant.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Node.js / npm (for MCP servers installed via `npx`)

## Quick start

```bash
cd demos/mcp_demo
uv sync
uv run uvicorn main:app --reload
# Open http://localhost:8000
```

## Try saying

- "What can you do?"
- "Create a file called shopping-list.txt with milk, eggs, and bread"
- "What files are there?"
- "Read shopping-list.txt"
- "Remember that my favorite color is blue"
- "What do you know about me?"

## Adding MCP servers

Create a `config.yaml` in this directory to override the default MCP servers:

```yaml
mcp:
  servers:
    # Keep the defaults
    - name: filesystem
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/mcp-workspace"]
    - name: memory
      command: npx
      args: ["-y", "@modelcontextprotocol/server-memory"]

    # Add more servers
    - name: fetch
      command: npx
      args: ["-y", "@modelcontextprotocol/server-fetch"]
    - name: time
      command: npx
      args: ["-y", "@modelcontextprotocol/server-time"]
```

Each entry needs:
- `name` — a label for the server (shown in the UI)
- `command` — the executable to run (`npx`, `uvx`, `python`, etc.)
- `args` — command-line arguments

The demo discovers tools automatically via the MCP protocol — no code changes needed.

### Popular MCP servers (no API key)

| Server | Command | What it does |
|--------|---------|-------------|
| Filesystem | `npx -y @modelcontextprotocol/server-filesystem /path` | File operations in a directory |
| Memory | `npx -y @modelcontextprotocol/server-memory` | Knowledge graph — remember things |
| Fetch | `npx -y @modelcontextprotocol/server-fetch` | Fetch and summarize any URL |
| Time | `npx -y @modelcontextprotocol/server-time` | Current time and timezone conversions |
| Git | `npx -y @modelcontextprotocol/server-git` | Read and search git repos |
| Everything | `npx -y @modelcontextprotocol/server-everything` | Test server with sample tools |

## TTS/STT tuning

The same `config.yaml` also supports TTS/STT overrides (shared across all demos):

```yaml
tts:
  padding_bonus: 1.5
stt:
  flush_duration_s: 0.8
session:
  silence_timeout_s: 5.0
```

See `demos/config.example.yaml` for all options.
