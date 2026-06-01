# claude-resume

A TUI session picker for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) `--resume`.

Browse, search, and resume previous Claude Code sessions from any project — without remembering session IDs.

[![PyPI version](https://img.shields.io/pypi/v/claude-resume)](https://pypi.org/project/claude-resume/)
[![Python](https://img.shields.io/pypi/pyversions/claude-resume)](https://pypi.org/project/claude-resume/)
[![Downloads](https://img.shields.io/pypi/dm/claude-resume)](https://pypi.org/project/claude-resume/)
[![License: MIT](https://img.shields.io/pypi/l/claude-resume)](LICENSE)

## Features

- **Session discovery** — Scans `~/.claude/projects/` for all sessions across every project
- **Real-time search** — Filter by project name, first prompt, or git branch
- **Project scoping** — Toggle between current project and all projects (`Ctrl+T`)
- **Agent filtering** — Hides SDK/subagent sessions by default; toggle with `a`
- **Sort modes** — Cycle through Modified / Messages / Project (`Ctrl+S`)
- **Session detail** — View full metadata with `Space`
- **Delete sessions** — Remove old sessions with `d`
- **Auto cd** — Automatically changes to the session's project directory before resuming
- **Fast cache** — Fingerprint-based cache for instant startup after first load
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
  -a, --include-agents Include SDK/agent (subagent) sessions, hidden by default
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
| `a` | Toggle agent/subagent sessions (hidden by default) |
| `Ctrl+S` | Cycle sort (Modified / Messages / Project) |
| `d` | Delete session (with confirmation) |
| `q` | Quit |

## How It Works

1. Scans `~/.claude/projects/*/sessions-index.json` for indexed sessions
2. Falls back to parsing `.jsonl` transcript files (reads first 50 lines + last line for efficiency)
3. Filters out sidechains and sessions without user messages
4. On selection, `cd`s to the session's project directory and `exec`s `claude --resume <id>`

## Requirements

- Python 3.10+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed
- [Textual](https://github.com/Textualize/textual) (installed automatically)

## License

MIT
