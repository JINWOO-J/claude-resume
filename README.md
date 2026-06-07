# claude-resume

A TUI session picker for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) `--resume`.

Browse, search, and resume previous Claude Code sessions from any project — without remembering session IDs.

[![PyPI version](https://img.shields.io/pypi/v/claude-resume)](https://pypi.org/project/claude-resume/)
[![Python](https://img.shields.io/pypi/pyversions/claude-resume)](https://pypi.org/project/claude-resume/)
[![Downloads](https://img.shields.io/pypi/dm/claude-resume)](https://pypi.org/project/claude-resume/)
[![License: MIT](https://img.shields.io/pypi/l/claude-resume)](LICENSE)

## Features

- **Session discovery** — Scans `~/.claude/projects/` for all sessions across every project
- **Real-time search** — Filter by project name, git branch, or message content (every prompt you typed, not just the first). Prefix with `r:` to search only the last assistant response
- **Project scoping** — Toggle between current project and all projects (`Ctrl+T`)
- **Agent filtering** — Hides agent/automation sessions (subagents, dispatched tasks, slash-command, heartbeat) by default; toggle with `a`
- **Recency-first default** — Default sort puts the most recently used session on top; the resumability ranking (human depth × recency, demoting one-turn and stale sessions) is one `Ctrl+S` away
- **Sort modes** — Cycle through Resumable / Modified / Messages / Project (`Ctrl+S`)
- **Readable rows** — Strips filler openings ("can you please…"), middle-truncates long prompts, color-codes recency, and groups by project (in Project sort)
- **Session detail** — View full metadata with `Space`
- **Delete sessions** — Remove old sessions with `d`
- **Auto cd** — Automatically changes to the session's project directory before resuming
- **Incremental cache** — Per-file cache; only changed transcripts are re-parsed, so a new session elsewhere no longer triggers a full rescan
- **CLI passthrough** — Extra arguments are forwarded to `claude`

## Installation

```bash
pip install claude-resume
```

Or install from source:

```bash
git clone https://github.com/jinwoo/claude-resume.git
cd claude-resume
pip install -e .
```

## Usage

```bash
claude-resume
```

### CLI Options

```
claude-resume [OPTIONS] [-- CLAUDE_ARGS...]

Options:
  -g, --global         Start in global (all projects) mode
  -l, --local          Start in local (current project) mode
  -a, --include-agents Include agent/automation sessions, hidden by default
  --no-cache           Force reload sessions without cache

Examples:
  claude-resume                     # Pick a session to resume
  claude-resume -g                  # Show all projects by default
  claude-resume --no-cache          # Ignore cache, rescan sessions
  claude-resume -- --verbose        # Pass --verbose to claude
```

### Key Bindings

| Key | Action |
|-----|--------|
| `Enter` | Resume selected session |
| `Space` | Show session detail |
| `/` | Focus search input |
| `Escape` | Clear search |
| `Ctrl+T` | Toggle scope (current project / all) |
| `a` | Toggle agent/automation sessions (hidden by default) |
| `Ctrl+S` | Cycle sort (Resumable / Modified / Messages / Project) |
| `d` | Delete session (with confirmation) |
| `q` | Quit |

## How It Works

1. Scans `~/.claude/projects/*/sessions-index.json` for indexed sessions
2. Parses `.jsonl` transcript files in a single pass (exact message counts and first prompt), caching each file by `(mtime, size)` so unchanged transcripts are never re-read
3. Filters out sidechains; classifies a session as agent/automation when no genuine human first prompt exists (slash commands, dispatched tasks, heartbeats, command output are not "genuine"), hiding those by default
4. On selection, `cd`s to the session's project directory and `exec`s `claude --resume <id>`

## Requirements

- Python 3.10+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed
- [Textual](https://github.com/Textualize/textual) (installed automatically)

## License

MIT
