"""TUI session picker for Claude Code resume."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Footer, Header, Input, Static
from rich.text import Text

CACHE_DIR = Path.home() / ".cache" / "claude-resume"
CACHE_FILE = CACHE_DIR / "sessions.json"
# Bump when the Session schema or extraction logic changes, to invalidate stale caches.
_SCHEMA_VERSION = "5"

# Cap on the cached human-message search blob, in characters, per session.
_SEARCH_TEXT_CAP = 1000
# Cap on the stored first prompt (the detail view compacts to 500 anyway). Keeps
# the cache small even when a session opens with a huge pasted/agent prompt.
_FIRST_PROMPT_CAP = 800


@dataclass
class Session:
    session_id: str
    project_name: str
    project_path: str
    first_prompt: str
    message_count: int
    git_branch: str
    created: str  # ISO format string for cache serialization
    modified: str  # ISO format string
    last_response: str = ""
    entrypoint: str = "cli"  # "cli" = main session, "sdk-cli" = agent/subagent session
    agent: bool = False  # content-based: no genuine human first prompt (dispatch/automation)
    human_msgs: int = 0  # non-meta user messages a human typed (for resumability score)
    search_text: str = ""  # lowercased blob of human messages, for content search

    @property
    def is_agent(self) -> bool:
        return self.agent or self.entrypoint == "sdk-cli"

    @property
    def created_dt(self) -> datetime:
        return _parse_iso(self.created)

    @property
    def modified_dt(self) -> datetime:
        return _parse_iso(self.modified)


def relative_time(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    return dt.strftime("%b %d")


def format_datetime(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _read_cache() -> dict[str, dict]:
    """Return the per-file cache map {abs_path: {mtime, size, session}} or {}.

    Keyed by jsonl path with its (mtime, size) so an unchanged file's parsed
    Session can be reused and only changed/new files re-parsed.
    """
    if not CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if data.get("schema") != _SCHEMA_VERSION:
        return {}  # schema changed → drop stale cache
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def _write_cache(files: dict[str, dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = {"schema": _SCHEMA_VERSION, "files": files}
    CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False))


def _save_cache(sessions: list[Session]) -> None:
    """Persist after an in-place mutation (e.g. delete): keep surviving ids only.

    Reuses the cached (mtime, size, session) of survivors so no file is re-read.
    """
    keep = {s.session_id for s in sessions}
    files = {
        path: entry for path, entry in _read_cache().items()
        if isinstance(entry.get("session"), dict)
        and entry["session"].get("session_id") in keep
    }
    _write_cache(files)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_from_index(index_file: Path) -> list[Session]:
    """Load sessions from a sessions-index.json file."""
    try:
        data = json.loads(index_file.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    sessions = []
    project_dir = index_file.parent
    for entry in data.get("entries", []):
        if entry.get("isSidechain", False):
            continue
        session_id = entry.get("sessionId", "")
        # Skip orphan sessions (no JSONL file)
        if not (project_dir / f"{session_id}.jsonl").exists():
            continue
        project_path = entry.get("projectPath", "")
        project_name = Path(project_path).name if project_path else project_dir.name
        try:
            # Validate dates parse correctly
            _parse_iso(entry["created"])
            _parse_iso(entry["modified"])
        except (KeyError, ValueError):
            continue
        raw_prompt = entry.get("firstPrompt", "")
        display, is_genuine = _classify_prompt(raw_prompt) if raw_prompt else ("", False)
        entrypoint = entry.get("entrypoint", "cli")
        sessions.append(Session(
            session_id=session_id,
            project_name=project_name,
            project_path=project_path,
            first_prompt=display or raw_prompt,
            message_count=entry.get("messageCount", 0),
            git_branch=entry.get("gitBranch", ""),
            created=entry["created"],
            modified=entry["modified"],
            entrypoint=entrypoint,
            agent=(entrypoint == "sdk-cli") or not is_genuine,
        ))
    return sessions


def _extract_first_user_prompt(msg: dict) -> str | None:
    """Extract text from a user message object."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return None


_CMD_NAME_RE = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
_CMD_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.S)

# Dispatch prompts emitted by agent-orchestration frameworks (term-mesh,
# sre-agent, …) rather than a human. Curated from observed transcripts; extend
# as new formats appear. Start-of-prompt prefixes:
_AGENT_PREFIXES = (
    "You are a team agent",           # term-mesh worker role assignment
    "## Task Capsule",                # term-mesh task dispatch
    "IME ROUTING REQUEST",            # term-mesh routing dispatch
    "Leader ping",                    # leader heartbeat
    "You did NOT actually run",       # agent self-correction nudge
    "[Request interrupted by user",   # interrupt marker — no instruction given
    "에이전트 핑퐁",                    # ping-pong benchmark (Korean)
)

# Substrings matched anywhere (they can trail stray human text / whitespace):
_AGENT_SUBSTRINGS = (
    "[REQUIRED FINAL STEP",  # team-dispatch task wrapper
    "--agent-id",            # `claude --agent-id …` agent launcher
    "핑퐁 체인",              # ping-pong relay benchmark (Korean)
)

# Agent heartbeat openers: bare ping/pong, PINGPONG, an uppercase PING opener,
# or ping/pong followed by punctuation or a heartbeat keyword. Tuned to leave a
# real human task like "ping the staging server" genuine.
_HEARTBEAT_RE = re.compile(
    r"ping\s*[.,:#)\-—]"               # "PING.", "PING —", "PING #1", "PING:"
    r"|ping\s+(?:from|check|connectivity|test)\b",  # "PING from", "Ping check"
    re.I,
)

# term-mesh dispatch opener: "Task <hexid> — ..." / "Task <hexid>: ...".
_DISPATCH_RE = re.compile(r"Task [0-9a-f]{6,}\b")

# System/command wrapper prefixes that are machine-emitted, not a human typing.
_SYSTEM_WRAPPER_PREFIXES = (
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<command-message>",
    "<command-name>",
    "<teammate-message",   # term-mesh agent-to-agent dispatch
    "<task-notification>",  # background task completion injected mid-session
    "<bash-input>",         # user ran a `!` shell command, not a task
    "<bash-stdout>",
    "<bash-stderr>",
)

# A session is classified human-vs-agent by its opening: only the first few
# typed (non-meta, text-bearing) user messages decide. This stops a dispatched
# task, paste, or task-notification deep in a long session from flipping an
# automation session to "human" (or vice versa).
_OPENING_USER_MSGS = 3


def _unwrap_command(text: str) -> tuple[str, str] | None:
    """If text is a slash-command wrapper, return (name, args); else None."""
    m = _CMD_NAME_RE.search(text)
    if not m:
        return None
    a = _CMD_ARGS_RE.search(text)
    return m.group(1).strip(), (a.group(1).strip() if a else "")


def _is_heartbeat(text: str) -> bool:
    low = text.lower()
    if low in ("ping", "pong") or low.startswith("pingpong"):
        return True
    if text.startswith("PING"):  # uppercase opener is an agent ping, not prose
        return True
    return bool(_HEARTBEAT_RE.match(text))


def _classify_prompt(text: str) -> tuple[str, bool]:
    """Map a user message body to (display_text, is_genuine).

    is_genuine means a human actually typed a content instruction. Slash
    commands (control plane: ``/watch``, ``/lib-mesh``, ``/clear`` …), injected
    dispatch/heartbeat prompts, command output, and empty bodies are all
    non-genuine — they are automation or housekeeping, not a human starting a
    session. Such sessions are hidden by default but kept (toggle with ``a``).
    """
    text = text.strip()
    if not text:
        return "", False
    cmd = _unwrap_command(text)
    if cmd is not None:
        name, args = cmd
        return f"{name} {args}".strip(), False
    if text.startswith(_AGENT_PREFIXES):
        return text, False
    if any(m in text for m in _AGENT_SUBSTRINGS):
        return text, False
    if _is_heartbeat(text):
        return text, False
    if _DISPATCH_RE.match(text):
        return text, False
    if text.startswith(_SYSTEM_WRAPPER_PREFIXES):
        return text, False
    return text, True


def _load_from_jsonl(jsonl_file: Path, project_dir: Path) -> Session | None:
    """Load session metadata from a JSONL transcript file.

    Scans the whole file in a single pass so the first human prompt is found
    even when it sits past the first dozen records (long agent system prompts,
    bulk attachments), and so message_count / human_msgs / last timestamp are
    exact rather than capped at an arbitrary read window.
    """
    session_id = jsonl_file.stem
    genuine = ""    # first genuine human instruction (display text)
    fallback = ""   # first non-genuine user text (bare command / injected dispatch)
    git_branch = ""
    cwd = ""
    entrypoint = ""
    first_ts = None
    last_ts = None
    user_msgs = 0
    assistant_msgs = 0
    human_msgs = 0     # genuine user messages over the whole session (for score)
    opening_seen = 0   # text-bearing non-meta user messages examined for classification
    search_parts: list[str] = []  # human message texts for content search
    search_len = 0

    try:
        with open(jsonl_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if obj.get("isSidechain", False):
                    return None
                if not git_branch:
                    git_branch = obj.get("gitBranch", "")
                if not cwd:
                    cwd = obj.get("cwd", "")
                if not entrypoint:
                    entrypoint = obj.get("entrypoint", "")
                ts = obj.get("timestamp")
                if ts:
                    try:
                        _parse_iso(ts)
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts
                    except ValueError:
                        pass

                t = obj.get("type", "")
                if t == "assistant":
                    assistant_msgs += 1
                elif t == "user":
                    user_msgs += 1
                    if obj.get("isMeta", False):
                        continue
                    msg = obj.get("message", {})
                    if not isinstance(msg, dict):
                        continue
                    text = _extract_first_user_prompt(msg)
                    if not (text and text.strip()):
                        continue
                    display, is_genuine = _classify_prompt(text)
                    if is_genuine:
                        human_msgs += 1
                        if search_len < _SEARCH_TEXT_CAP:
                            search_parts.append(display[:_SEARCH_TEXT_CAP].lower())
                            search_len += len(display) + 1
                    # Decide human-vs-agent from the opening only.
                    if not genuine and opening_seen < _OPENING_USER_MSGS:
                        opening_seen += 1
                        if is_genuine:
                            genuine = display
                        elif not fallback:
                            fallback = display
    except OSError:
        return None

    first_prompt = (genuine or fallback)[:_FIRST_PROMPT_CAP]
    if first_ts is None or not first_prompt:
        return None

    # Agent (hidden by default) when launched as a subagent, or when no human
    # ever typed a genuine first instruction (dispatch / heartbeat / automation).
    is_agent = (entrypoint == "sdk-cli") or not genuine

    project_path = cwd if cwd else ""
    project_name = Path(project_path).name if project_path else project_dir.name

    return Session(
        session_id=session_id,
        project_name=project_name,
        project_path=project_path,
        first_prompt=first_prompt,
        message_count=user_msgs + assistant_msgs,
        git_branch=git_branch,
        created=first_ts,
        modified=last_ts or first_ts,
        entrypoint=entrypoint or "cli",
        agent=is_agent,
        human_msgs=human_msgs,
        search_text=" ".join(search_parts)[:_SEARCH_TEXT_CAP],
    )


def load_all_sessions(no_cache: bool = False) -> list[Session]:
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return []

    cache = {} if no_cache else _read_cache()
    sessions: list[Session] = []
    seen_ids: set[str] = set()
    new_files: dict[str, dict] = {}

    # 1) Load from sessions-index.json (preferred, has accurate counts).
    #    Rare in practice and not incrementally cached.
    for index_file in claude_dir.glob("*/sessions-index.json"):
        for s in _load_from_index(index_file):
            if s.session_id in seen_ids:
                continue
            if not s.last_response:
                s.last_response = _get_last_assistant_response(s.session_id)
            sessions.append(s)
            seen_ids.add(s.session_id)

    # 2) Scan JSONL files, reusing cached entries for files that haven't changed.
    for project_dir in claude_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            sid = jsonl_file.stem
            if sid in seen_ids:
                continue
            path = str(jsonl_file)
            try:
                st = jsonl_file.stat()
            except OSError:
                continue

            session: Session | None = None
            entry = cache.get(path)
            if (entry and entry.get("mtime") == st.st_mtime
                    and entry.get("size") == st.st_size
                    and isinstance(entry.get("session"), dict)):
                try:
                    session = Session(**entry["session"])  # reuse: no re-read
                except (TypeError, ValueError):
                    session = None
            if session is None:
                session = _load_from_jsonl(jsonl_file, project_dir)
                if session and not session.last_response:
                    session.last_response = _get_last_assistant_response(session.session_id)

            if session:
                sessions.append(session)
                seen_ids.add(sid)
                new_files[path] = {"mtime": st.st_mtime, "size": st.st_size,
                                   "session": asdict(session)}

    sessions.sort(key=lambda s: _parse_iso(s.modified), reverse=True)

    if not no_cache:
        _write_cache(new_files)

    return sessions


def delete_session(session: Session) -> bool:
    """Delete a session's JSONL file."""
    claude_dir = Path.home() / ".claude" / "projects"
    for jsonl_file in claude_dir.rglob(f"{session.session_id}.jsonl"):
        try:
            jsonl_file.unlink()
            return True
        except OSError:
            return False
    return False


def _find_session_jsonl(session_id: str) -> Path | None:
    """Find the JSONL file for a session ID."""
    claude_dir = Path.home() / ".claude" / "projects"
    for f in claude_dir.rglob(f"{session_id}.jsonl"):
        return f
    return None


def _truncate(text: str, limit: int = 35) -> str:
    """Collapse whitespace and truncate with ellipsis."""
    t = " ".join(text.split())
    return t[:limit] + ("..." if len(t) > limit else "")


# Leading filler that hides the actual topic ("can you please fix ..." → "fix ...").
_FILLER_RE = re.compile(
    r"^(?:please|pls|can you|could you|would you|will you|help me( to)?|"
    r"i want to|i'd like to|i would like to|let'?s|let me|hey|hi|so|okay|ok)\b[\s,]*",
    re.I,
)


def _strip_filler(text: str) -> str:
    """Drop leading conversational filler so the topic leads the column."""
    t = text.strip()
    prev = None
    while t and t != prev:
        prev = t
        t = _FILLER_RE.sub("", t, count=1).strip()
    return t or text.strip()


def _truncate_middle(text: str, limit: int = 40) -> str:
    """Collapse whitespace and truncate with a middle ellipsis (head-weighted).

    Keeps the topic start and the tail visible so near-identical openings stay
    distinguishable by their ending.
    """
    t = " ".join(text.split())
    if len(t) <= limit:
        return t
    if limit <= 3:
        return t[:limit]
    keep = limit - 1  # one char for the ellipsis
    head = keep * 2 // 3
    tail = keep - head
    return t[:head] + "…" + t[-tail:]


def _recency_style(age_days: float) -> str:
    """Rich style for the When column, brighter the more recent."""
    if age_days < 1:
        return "bold green"
    if age_days < 7:
        return ""
    if age_days < 30:
        return "dim"
    return "dim italic"


def _first_paragraph(text: str) -> str:
    """Extract the first non-empty paragraph from text."""
    for para in text.split("\n\n"):
        stripped = para.strip()
        if stripped:
            # Collapse to single line
            return " ".join(stripped.split())
    return text.strip()


def _get_last_assistant_response(session_id: str) -> str:
    """Extract the first paragraph of the last assistant response."""
    filepath = _find_session_jsonl(session_id)
    if not filepath:
        return ""
    try:
        with open(filepath, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(262144, size)  # last 256KB
            f.seek(-chunk, 2)
            data = f.read()
        for line in reversed(data.split(b"\n")):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message", {})
            if not isinstance(msg, dict):
                continue
            text = _extract_text(msg)
            if text:
                return _first_paragraph(text)
    except OSError:
        pass
    return ""


def _extract_text(msg: dict) -> str:
    """Extract text content from a message object."""
    content = msg.get("content", "")
    if isinstance(content, str) and content:
        return content.strip()
    if isinstance(content, list):
        texts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                texts.append(c["text"].strip())
        return "\n".join(texts) if texts else ""
    return ""


def get_last_messages(session_id: str) -> tuple[str, str]:
    """Extract the last user message and last assistant response.

    Returns (last_user, last_assistant).
    """
    filepath = _find_session_jsonl(session_id)
    if not filepath:
        return "(file not found)", "(file not found)"
    last_user = ""
    last_assistant = ""
    try:
        with open(filepath, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(262144, size)  # last 256KB
            f.seek(-chunk, 2)
            data = f.read()
        lines = data.split(b"\n")
        for line in reversed(lines):
            if last_user and last_assistant:
                break
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_type = obj.get("type", "")
            if msg_type == "user" and not last_user:
                msg = obj.get("message", {})
                if isinstance(msg, dict):
                    text = _extract_text(msg)
                    if text:
                        last_user = text
            elif msg_type == "assistant" and not last_assistant:
                msg = obj.get("message", {})
                if isinstance(msg, dict):
                    text = _extract_text(msg)
                    if text:
                        last_assistant = text
    except OSError:
        pass
    return last_user or "(no messages)", last_assistant or "(no response)"


def detect_current_project() -> str | None:
    try:
        return str(Path.cwd().resolve())
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Sort modes
# ---------------------------------------------------------------------------

SORT_MODES = ["resume", "modified", "messages", "project"]
SORT_LABELS = {"resume": "Resumable", "modified": "Modified",
               "messages": "Messages", "project": "Project"}


def resumability_score(s: Session, now: datetime | None = None) -> float:
    """Rank a session by how worth resuming it is: human depth × recency.

    A one-turn question or a session untouched for weeks scores low; a long,
    recently-active session scores high. Non-human turns count a fraction of a
    genuine turn so a session padded with tool results doesn't outrank a real
    conversation.
    """
    now = now or datetime.now(timezone.utc)
    age_days = max(0.0, (now - s.modified_dt).total_seconds() / 86400.0)
    recency = math.exp(-age_days / 14.0)  # ~0.61 at 1wk, ~0.37 at 2wk
    other = max(0, s.message_count - s.human_msgs)
    depth = s.human_msgs + 0.2 * other
    return (depth + 1.0) * recency


def sort_sessions(sessions: list[Session], mode: str) -> list[Session]:
    if mode == "messages":
        return sorted(sessions, key=lambda s: s.message_count, reverse=True)
    elif mode == "project":
        return sorted(sessions, key=lambda s: (s.project_name.lower(), s.modified_dt), reverse=False)
    elif mode == "modified":
        return sorted(sessions, key=lambda s: s.modified_dt, reverse=True)
    else:  # resume (default)
        now = datetime.now(timezone.utc)
        return sorted(sessions, key=lambda s: resumability_score(s, now), reverse=True)


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------

class ConfirmDeleteScreen(ModalScreen[bool]):
    """Confirmation dialog for session deletion."""

    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "Cancel"),
    ]

    CSS = """
    ConfirmDeleteScreen {
        align: center middle;
    }
    #confirm-box {
        width: 60;
        height: auto;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    .btn-row {
        margin-top: 1;
        height: 3;
    }
    .btn-row Button {
        margin: 0 1;
    }
    """

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        prompt = _truncate(self.session.first_prompt, 40)
        with Vertical(id="confirm-box"):
            yield Static("[b]Delete session?[/b]\n")
            yield Static(f"{self.session.project_name} / {prompt}")
            with Horizontal(classes="btn-row"):
                yield Button("Delete (y)", variant="error", id="btn-yes")
                yield Button("Cancel (n)", variant="default", id="btn-no")

    @on(Button.Pressed, "#btn-yes")
    def on_yes(self, event: Button.Pressed) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn-no")
    def on_no(self, event: Button.Pressed) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class DetailScreen(ModalScreen[None]):
    """Modal screen showing session details."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("space", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
    ]

    CSS = """
    DetailScreen {
        align: center middle;
    }
    #detail-box {
        width: 90%;
        max-width: 100;
        height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
        overflow-y: auto;
    }
    .prompt-box {
        background: $panel;
        border: round $primary-background;
        padding: 1 2;
        margin: 1 0;
    }
    """

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        s = self.session
        first = s.first_prompt if s.first_prompt else "(no prompt)"
        last_user, last_assistant = get_last_messages(s.session_id)
        def _compact(text: str, limit: int = 500) -> str:
            """Strip leading/trailing whitespace and collapse multiple blank lines."""
            t = text.strip()
            t = re.sub(r"\n{3,}", "\n\n", t)
            return t[:limit] + ("..." if len(t) > limit else "")

        first_display = _compact(first)
        last_user_display = _compact(last_user)
        last_asst_display = _compact(last_assistant)
        with Vertical(id="detail-box"):
            yield Static("[b]Session Detail[/b]\n")
            yield Static(f"[dim]Session ID[/]   {s.session_id}")
            yield Static(f"[dim]Project[/]      {s.project_name}")
            yield Static(f"[dim]Path[/]         {s.project_path}")
            yield Static(f"[dim]Branch[/]       {s.git_branch}")
            yield Static(f"[dim]Messages[/]     {s.message_count}")
            yield Static(f"[dim]Created[/]      {format_datetime(s.created_dt)}")
            yield Static(f"[dim]Modified[/]     {format_datetime(s.modified_dt)} ({relative_time(s.modified_dt)})")
            yield Static("\n[dim]Last Assistant Response[/]")
            yield Static(last_asst_display, classes="prompt-box")
            yield Static("[dim]Last User Message[/]")
            yield Static(last_user_display, classes="prompt-box")
            yield Static("[dim]First Prompt[/]")
            yield Static(first_display, classes="prompt-box")


class SessionPicker(App):
    CSS = """
    #search {
        dock: top;
        margin: 0 1;
    }
    #scope-bar {
        dock: top;
        margin: 0 1;
        height: 1;
        color: $text-muted;
    }
    #table {
        margin: 0 1;
    }
    #empty {
        margin: 2 4;
        color: $text-muted;
    }
    """

    TITLE = "Claude Resume Picker"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("slash", "focus_search", "Search", key_display="/"),
        Binding("escape", "clear_search", "Clear"),
        Binding("ctrl+t", "toggle_scope", "Scope", key_display="^T"),
        Binding("ctrl+s", "cycle_sort", "Sort", key_display="^S"),
        Binding("space", "show_detail", "Detail", key_display="Space"),
        Binding("d", "delete_session", "Delete", key_display="d"),
        Binding("i", "toggle_full_id", "Full ID", key_display="i"),
        Binding("a", "toggle_agents", "Agents", key_display="a"),
    ]

    global_mode: reactive[bool] = reactive(False)
    show_agents: reactive[bool] = reactive(False)

    def __init__(self, initial_global: bool = False, sessions: list[Session] | None = None,
                 full_id: bool = False, show_agents: bool = False) -> None:
        super().__init__()
        self.all_sessions = sessions if sessions is not None else load_all_sessions()
        self.filtered_sessions: list[Session] = []
        self.selected_session: Session | None = None
        self.current_project = detect_current_project()
        self.sort_mode = "modified"
        self._init_global = initial_global
        self.full_id = full_id
        self._init_show_agents = show_agents
        if not self._init_global and self.current_project:
            local = [s for s in self.all_sessions
                     if s.project_path == self.current_project and not s.is_agent]
            if not local:
                self._init_global = True

    def _get_scope_sessions(self) -> list[Session]:
        if self.global_mode or not self.current_project:
            sessions = list(self.all_sessions)
        else:
            sessions = [s for s in self.all_sessions if s.project_path == self.current_project]
        if not self.show_agents:
            sessions = [s for s in sessions if not s.is_agent]
        return sessions

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Search sessions (project, prompt, branch, id)...", id="search")
        yield Static("", id="scope-bar")
        if not self.all_sessions:
            yield Static("No sessions found. Start a Claude Code session first.", id="empty")
        else:
            yield DataTable(id="table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        if self.all_sessions:
            table = self.query_one("#table", DataTable)
            table.add_columns("ID", "Project", "First Prompt", "Last Response", "Msgs", "Branch", "When")
        if self._init_global:
            self.global_mode = True
        if self._init_show_agents:
            self.show_agents = True
        self._apply_filter()
        if self.all_sessions:
            self.query_one("#table", DataTable).focus()

    def _update_scope_bar(self) -> None:
        try:
            bar = self.query_one("#scope-bar", Static)
        except Exception:
            return
        if self.global_mode or not self.current_project:
            scope = "All Projects"
        else:
            scope = Path(self.current_project).name
        count = len(self.filtered_sessions)
        sort_label = SORT_LABELS[self.sort_mode]
        agents = "on" if self.show_agents else "off"
        bar.update(f" [{scope}] {count} sessions | Sort: {sort_label} | Agents: {agents}  (^T: scope  ^S: sort  a: agents)")

    def _apply_filter(self) -> None:
        scope_sessions = self._get_scope_sessions()
        try:
            search = self.query_one("#search", Input).value.lower().strip()
        except Exception:
            search = ""
        if search.startswith("r:"):
            # response-only search
            term = search[2:].strip()
            self.filtered_sessions = [
                s for s in scope_sessions if term and term in s.last_response.lower()
            ] if term else scope_sessions
        elif search:
            self.filtered_sessions = [
                s for s in scope_sessions
                if search in s.project_name.lower()
                or search in s.first_prompt.lower()
                or search in s.last_response.lower()
                or search in s.git_branch.lower()
                or search in s.session_id.lower()
                or search in s.search_text
            ]
        else:
            self.filtered_sessions = scope_sessions
        self.filtered_sessions = sort_sessions(self.filtered_sessions, self.sort_mode)
        if self.all_sessions:
            self._populate_table()
        self._update_scope_bar()

    def _populate_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        now = datetime.now(timezone.utc)
        group_sort = self.sort_mode == "project"
        if group_sort:
            counts: dict[str, int] = {}
            for s in self.filtered_sessions:
                counts[s.project_name] = counts.get(s.project_name, 0) + 1
        prev_project = None
        for s in self.filtered_sessions:
            prompt = _truncate_middle(_strip_filler(s.first_prompt), 40)
            response = _truncate(s.last_response, 35) if s.last_response.strip() else ""
            sid = s.session_id if self.full_id else s.session_id[:8]
            if group_sort:
                if s.project_name != prev_project:
                    project_cell = Text(f"{s.project_name} ({counts[s.project_name]})", style="bold")
                    prev_project = s.project_name
                else:
                    project_cell = Text(s.project_name, style="dim")
            else:
                project_cell = s.project_name
            age_days = (now - s.modified_dt).total_seconds() / 86400.0
            when_cell = Text(relative_time(s.modified_dt), style=_recency_style(age_days))
            table.add_row(
                sid,
                project_cell,
                prompt,
                response,
                str(s.message_count),
                s.git_branch,
                when_cell,
                key=s.session_id,
            )

    def watch_global_mode(self, value: bool) -> None:
        try:
            _ = self.screen
        except Exception:
            return
        if self.is_mounted:
            self._apply_filter()

    def watch_show_agents(self, value: bool) -> None:
        try:
            _ = self.screen
        except Exception:
            return
        if self.is_mounted:
            self._apply_filter()

    @on(Input.Changed, "#search")
    def filter_sessions(self, event: Input.Changed) -> None:
        self._apply_filter()

    @on(DataTable.RowSelected, "#table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            session_id = event.row_key.value
            for s in self.filtered_sessions:
                if s.session_id == session_id:
                    self.selected_session = s
                    break
            self.exit()

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_clear_search(self) -> None:
        search = self.query_one("#search", Input)
        if search.value:
            search.value = ""
        if self.all_sessions:
            self.query_one("#table", DataTable).focus()

    def action_toggle_scope(self) -> None:
        self.global_mode = not self.global_mode

    def action_toggle_agents(self) -> None:
        self.show_agents = not self.show_agents

    def action_cycle_sort(self) -> None:
        idx = SORT_MODES.index(self.sort_mode)
        self.sort_mode = SORT_MODES[(idx + 1) % len(SORT_MODES)]
        self._apply_filter()

    def action_show_detail(self) -> None:
        if not self.all_sessions or not self.filtered_sessions:
            return
        table = self.query_one("#table", DataTable)
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self.filtered_sessions):
            return
        self.push_screen(DetailScreen(self.filtered_sessions[row_idx]))

    def action_delete_session(self) -> None:
        if not self.all_sessions or not self.filtered_sessions:
            return
        table = self.query_one("#table", DataTable)
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self.filtered_sessions):
            return
        session = self.filtered_sessions[row_idx]

        def on_confirm(result: bool) -> None:
            if not result:
                return
            if delete_session(session):
                self.all_sessions = [s for s in self.all_sessions if s.session_id != session.session_id]
                _save_cache(self.all_sessions)
                self._apply_filter()
                self.notify("Session deleted", severity="information")
            else:
                self.notify("Failed to delete session", severity="error")

        self.push_screen(ConfirmDeleteScreen(session), on_confirm)

    def action_toggle_full_id(self) -> None:
        self.full_id = not self.full_id
        self._populate_table()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="TUI session picker for Claude Code resume",
        epilog="All other arguments are passed through to the claude CLI.",
    )
    parser.add_argument("--global", "-g", dest="global_mode", action="store_true",
                        help="Start in global (all projects) mode")
    parser.add_argument("--local", "-l", dest="local_mode", action="store_true",
                        help="Start in local (current project) mode")
    parser.add_argument("--no-cache", action="store_true",
                        help="Force reload sessions without cache")
    parser.add_argument("--list", action="store_true",
                        help="List sessions as plain text (no TUI)")
    parser.add_argument("--full-id", action="store_true",
                        help="Show full session ID instead of short 8-char ID")
    parser.add_argument("--include-agents", "-a", action="store_true",
                        help="Include agent/automation sessions (subagents, dispatch, "
                             "slash-command, heartbeat), hidden by default")
    args, extra_args = parser.parse_known_args()

    sessions = load_all_sessions(no_cache=args.no_cache)

    initial_global = args.global_mode and not args.local_mode

    if args.list:
        current_project = detect_current_project()
        if not initial_global and current_project:
            sessions = [s for s in sessions if s.project_path == current_project]
        if not args.include_agents:
            sessions = [s for s in sessions if not s.is_agent]
        for s in sessions:
            sid = s.session_id if args.full_id else s.session_id[:8]
            print(f"{sid}  {s.project_name:<20s}  {_truncate(s.first_prompt, 40):<43s}  {relative_time(s.modified_dt)}")
        sys.exit(0)

    app = SessionPicker(initial_global=initial_global, sessions=sessions,
                        full_id=args.full_id, show_agents=args.include_agents)
    app.run()

    if app.selected_session:
        s = app.selected_session
        if s.project_path and os.path.isdir(s.project_path):
            os.chdir(s.project_path)

        cmd = ["claude", "--resume", s.session_id] + extra_args
        print(f"cd {s.project_path}")
        print(f"claude --resume {s.session_id}" + (f" {' '.join(extra_args)}" if extra_args else ""))
        os.execvp("claude", cmd)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
