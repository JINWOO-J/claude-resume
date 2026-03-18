"""TUI session picker for Claude Code resume."""

from __future__ import annotations

import argparse
import hashlib
import json
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

CACHE_DIR = Path.home() / ".cache" / "claude-resume"
CACHE_FILE = CACHE_DIR / "sessions.json"


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

def _projects_fingerprint() -> str:
    """Build a fingerprint from project dirs' mtime + jsonl file counts."""
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return ""
    parts = []
    for d in sorted(claude_dir.iterdir()):
        if d.is_dir():
            try:
                mtime = d.stat().st_mtime
                jsonl_count = sum(1 for _ in d.glob("*.jsonl"))
                parts.append(f"{d.name}:{mtime:.0f}:{jsonl_count}")
            except OSError:
                pass
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _load_cache() -> list[Session] | None:
    """Load sessions from cache if fingerprint matches."""
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
        if data.get("fingerprint") != _projects_fingerprint():
            return None
        return [Session(**e) for e in data["sessions"]]
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        return None


def _save_cache(sessions: list[Session]) -> None:
    """Save sessions to cache with fingerprint."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "fingerprint": _projects_fingerprint(),
        "sessions": [asdict(s) for s in sessions],
    }
    CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False))


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
        sessions.append(Session(
            session_id=session_id,
            project_name=project_name,
            project_path=project_path,
            first_prompt=entry.get("firstPrompt", ""),
            message_count=entry.get("messageCount", 0),
            git_branch=entry.get("gitBranch", ""),
            created=entry["created"],
            modified=entry["modified"],
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


def _read_last_line(filepath: Path) -> str | None:
    """Read the last line of a file efficiently by seeking from end."""
    try:
        with open(filepath, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return None
            chunk = min(8192, size)
            f.seek(-chunk, 2)
            data = f.read()
            lines = data.split(b"\n")
            for line in reversed(lines):
                if line.strip():
                    return line.decode("utf-8", errors="replace")
    except OSError:
        pass
    return None


def _load_from_jsonl(jsonl_file: Path, project_dir: Path) -> Session | None:
    """Load session metadata from a JSONL transcript file."""
    session_id = jsonl_file.stem
    first_prompt = ""
    git_branch = ""
    cwd = ""
    first_ts = None
    msg_count = 0

    try:
        with open(jsonl_file, "r") as f:
            for i, line in enumerate(f):
                if i >= 50:
                    break
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
                if first_ts is None:
                    ts = obj.get("timestamp")
                    if ts:
                        try:
                            _parse_iso(ts)
                            first_ts = ts
                        except ValueError:
                            pass

                t = obj.get("type", "")
                if t in ("user", "assistant"):
                    msg_count += 1
                if t == "user" and not first_prompt:
                    msg = obj.get("message", {})
                    if isinstance(msg, dict):
                        text = _extract_first_user_prompt(msg)
                        if text:
                            first_prompt = text
    except OSError:
        return None

    if first_ts is None or not first_prompt:
        return None

    # Get last timestamp from end of file
    last_ts = first_ts
    last_line = _read_last_line(jsonl_file)
    if last_line:
        try:
            last_obj = json.loads(last_line)
            ts = last_obj.get("timestamp")
            if ts:
                _parse_iso(ts)  # validate
                last_ts = ts
        except (json.JSONDecodeError, ValueError):
            pass

    project_path = cwd if cwd else ""
    project_name = Path(project_path).name if project_path else project_dir.name

    return Session(
        session_id=session_id,
        project_name=project_name,
        project_path=project_path,
        first_prompt=first_prompt,
        message_count=msg_count,
        git_branch=git_branch,
        created=first_ts,
        modified=last_ts,
    )


def load_all_sessions(no_cache: bool = False) -> list[Session]:
    # Try cache first
    if not no_cache:
        cached = _load_cache()
        if cached is not None:
            return cached

    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return []

    sessions: list[Session] = []
    indexed_session_ids: set[str] = set()

    # 1) Load from sessions-index.json (preferred, has accurate counts)
    for index_file in claude_dir.glob("*/sessions-index.json"):
        for s in _load_from_index(index_file):
            sessions.append(s)
            indexed_session_ids.add(s.session_id)

    # 2) Scan JSONL files not covered by any index
    for project_dir in claude_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            sid = jsonl_file.stem
            if sid in indexed_session_ids:
                continue
            session = _load_from_jsonl(jsonl_file, project_dir)
            if session:
                sessions.append(session)
                indexed_session_ids.add(session.session_id)

    # Fill last_response for each session
    for s in sessions:
        if not s.last_response:
            s.last_response = _get_last_assistant_response(s.session_id)

    sessions.sort(key=lambda s: _parse_iso(s.modified), reverse=True)

    # Save cache
    _save_cache(sessions)

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

SORT_MODES = ["modified", "messages", "project"]
SORT_LABELS = {"modified": "Modified", "messages": "Messages", "project": "Project"}


def sort_sessions(sessions: list[Session], mode: str) -> list[Session]:
    if mode == "messages":
        return sorted(sessions, key=lambda s: s.message_count, reverse=True)
    elif mode == "project":
        return sorted(sessions, key=lambda s: (s.project_name.lower(), s.modified_dt), reverse=False)
    else:  # modified
        return sorted(sessions, key=lambda s: s.modified_dt, reverse=True)


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
    ]

    global_mode: reactive[bool] = reactive(False)

    def __init__(self, initial_global: bool = False, sessions: list[Session] | None = None, full_id: bool = False) -> None:
        super().__init__()
        self.all_sessions = sessions if sessions is not None else load_all_sessions()
        self.filtered_sessions: list[Session] = []
        self.selected_session: Session | None = None
        self.current_project = detect_current_project()
        self.sort_mode = "modified"
        self._init_global = initial_global
        self.full_id = full_id
        if not self._init_global and self.current_project:
            local = [s for s in self.all_sessions if s.project_path == self.current_project]
            if not local:
                self._init_global = True

    def _get_scope_sessions(self) -> list[Session]:
        if self.global_mode or not self.current_project:
            return list(self.all_sessions)
        return [s for s in self.all_sessions if s.project_path == self.current_project]

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
        bar.update(f" [{scope}] {count} sessions | Sort: {sort_label}  (^T: scope  ^S: sort)")

    def _apply_filter(self) -> None:
        scope_sessions = self._get_scope_sessions()
        try:
            search = self.query_one("#search", Input).value.lower().strip()
        except Exception:
            search = ""
        if search:
            self.filtered_sessions = [
                s for s in scope_sessions
                if search in s.project_name.lower()
                or search in s.first_prompt.lower()
                or search in s.last_response.lower()
                or search in s.git_branch.lower()
                or search in s.session_id.lower()
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
        for s in self.filtered_sessions:
            prompt = _truncate(s.first_prompt, 35)
            response = _truncate(s.last_response, 35) if s.last_response.strip() else ""
            sid = s.session_id if self.full_id else s.session_id[:8]
            table.add_row(
                sid,
                s.project_name,
                prompt,
                response,
                str(s.message_count),
                s.git_branch,
                relative_time(s.modified_dt),
                key=s.session_id,
            )

    def watch_global_mode(self, value: bool) -> None:
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
    args, extra_args = parser.parse_known_args()

    sessions = load_all_sessions(no_cache=args.no_cache)

    initial_global = args.global_mode and not args.local_mode

    if args.list:
        current_project = detect_current_project()
        if not initial_global and current_project:
            sessions = [s for s in sessions if s.project_path == current_project]
        for s in sessions:
            sid = s.session_id if args.full_id else s.session_id[:8]
            print(f"{sid}  {s.project_name:<20s}  {_truncate(s.first_prompt, 40):<43s}  {relative_time(s.modified_dt)}")
        sys.exit(0)

    app = SessionPicker(initial_global=initial_global, sessions=sessions, full_id=args.full_id)
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
