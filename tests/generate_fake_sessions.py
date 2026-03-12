"""Generate 10 fake Claude Code sessions for testing claude-resume."""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

FAKE_SESSIONS = [
    {
        "project_name": "claude-resume",
        "project_path": "/Users/jinwoo/work/project/claude-resume",
        "first_prompt": "Add column resize feature to the DataTable",
        "git_branch": "feat/column-resize",
        "message_count": 15,
        "hours_ago": 1,
    },
    {
        "project_name": "claude-resume",
        "project_path": "/Users/jinwoo/work/project/claude-resume",
        "first_prompt": "Add bookmark/favorite feature to session list",
        "git_branch": "feat/bookmarks",
        "message_count": 22,
        "hours_ago": 3,
    },
    {
        "project_name": "claude-resume",
        "project_path": "/Users/jinwoo/work/project/claude-resume",
        "first_prompt": "Add date range filter option to search",
        "git_branch": "feat/date-filter",
        "message_count": 18,
        "hours_ago": 6,
    },
    {
        "project_name": "claude-resume",
        "project_path": "/Users/jinwoo/work/project/claude-resume",
        "first_prompt": "Fix delete confirmation dialog not showing up",
        "git_branch": "fix/delete-confirm",
        "message_count": 8,
        "hours_ago": 12,
    },
    {
        "project_name": "claude-resume",
        "project_path": "/Users/jinwoo/work/project/claude-resume",
        "first_prompt": "Set up pytest config in pyproject.toml and create test structure",
        "git_branch": "chore/test-setup",
        "message_count": 34,
        "hours_ago": 24,
    },
    {
        "project_name": "claude-resume",
        "project_path": "/Users/jinwoo/work/project/claude-resume",
        "first_prompt": "Debug cache fingerprint not detecting directory changes",
        "git_branch": "fix/cache-fingerprint",
        "message_count": 42,
        "hours_ago": 48,
    },
    {
        "project_name": "claude-resume",
        "project_path": "/Users/jinwoo/work/project/claude-resume",
        "first_prompt": "Profile and optimize JSONL parsing for large files",
        "git_branch": "perf/jsonl-parsing",
        "message_count": 56,
        "hours_ago": 72,
    },
    {
        "project_name": "claude-resume",
        "project_path": "/Users/jinwoo/work/project/claude-resume",
        "first_prompt": "Fix table row selection color not visible in dark mode",
        "git_branch": "fix/dark-mode-colors",
        "message_count": 11,
        "hours_ago": 120,
    },
    {
        "project_name": "claude-resume",
        "project_path": "/Users/jinwoo/work/project/claude-resume",
        "first_prompt": "Set up GitHub Actions CI pipeline with lint, test, and build",
        "git_branch": "chore/ci-setup",
        "message_count": 29,
        "hours_ago": 168,
    },
    {
        "project_name": "claude-resume",
        "project_path": "/Users/jinwoo/work/project/claude-resume",
        "first_prompt": "Add screenshots and GIF demo to README",
        "git_branch": "docs/readme-demo",
        "message_count": 7,
        "hours_ago": 240,
    },
]


def _make_session_id() -> str:
    return str(uuid.uuid4())


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _project_dir_name(project_path: str) -> str:
    """Convert project path to Claude's directory naming convention."""
    return project_path.replace("/", "-")


def generate_jsonl(session: dict, session_id: str, now: datetime) -> list[str]:
    """Generate JSONL lines mimicking a real Claude Code session transcript."""
    created = now - timedelta(hours=session["hours_ago"])
    modified = created + timedelta(minutes=session["message_count"] * 2)

    lines = []

    # First line: session metadata
    lines.append(json.dumps({
        "type": "system",
        "timestamp": _iso(created),
        "sessionId": session_id,
        "cwd": session["project_path"],
        "gitBranch": session["git_branch"],
        "isSidechain": False,
    }))

    # User message (first prompt)
    lines.append(json.dumps({
        "type": "user",
        "timestamp": _iso(created + timedelta(seconds=1)),
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": session["first_prompt"]}],
        },
    }))

    # Simulate some back-and-forth
    for i in range(1, min(session["message_count"], 10)):
        t = created + timedelta(minutes=i * 2)
        if i % 2 == 1:
            lines.append(json.dumps({
                "type": "assistant",
                "timestamp": _iso(t),
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"(assistant response {i})"}],
                },
            }))
        else:
            lines.append(json.dumps({
                "type": "user",
                "timestamp": _iso(t),
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": f"(follow-up message {i})"}],
                },
            }))

    # Last line with final timestamp
    lines.append(json.dumps({
        "type": "assistant",
        "timestamp": _iso(modified),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "(final response)"}],
        },
    }))

    return lines


def generate_index_entry(session: dict, session_id: str, now: datetime) -> dict:
    created = now - timedelta(hours=session["hours_ago"])
    modified = created + timedelta(minutes=session["message_count"] * 2)
    return {
        "sessionId": session_id,
        "projectPath": session["project_path"],
        "firstPrompt": session["first_prompt"],
        "messageCount": session["message_count"],
        "gitBranch": session["git_branch"],
        "created": _iso(created),
        "modified": _iso(modified),
        "isSidechain": False,
    }


def main():
    output_base = Path.home() / ".claude" / "projects"
    output_base.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)

    # Group sessions by project path
    projects: dict[str, list[tuple[dict, str]]] = {}
    for session in FAKE_SESSIONS:
        sid = _make_session_id()
        dir_name = _project_dir_name(session["project_path"])
        projects.setdefault(dir_name, []).append((session, sid))

    for dir_name, session_list in projects.items():
        project_dir = output_base / dir_name
        project_dir.mkdir(parents=True, exist_ok=True)

        # Write JSONL files
        index_entries = []
        for session, sid in session_list:
            jsonl_path = project_dir / f"{sid}.jsonl"
            lines = generate_jsonl(session, sid, now)
            jsonl_path.write_text("\n".join(lines) + "\n")
            index_entries.append(generate_index_entry(session, sid, now))

        # Write sessions-index.json
        index_path = project_dir / "sessions-index.json"
        index_path.write_text(json.dumps({"entries": index_entries}, indent=2, ensure_ascii=False))

    print(f"Generated {len(FAKE_SESSIONS)} fake sessions in: {output_base}")
    print(f"\nTo test, symlink or copy to ~/.claude/projects/:")
    print(f"  cp -r {output_base}/* ~/.claude/projects/")
    print(f"\nOr modify CLAUDE_DIR in claude_resume.py to point here for testing.")

    # Print summary
    print(f"\n{'='*60}")
    print(f"{'Project':<20} {'Branch':<28} {'Msgs':>5}  {'Age'}")
    print(f"{'-'*60}")
    for s in FAKE_SESSIONS:
        h = s["hours_ago"]
        if h < 24:
            age = f"{h}h ago"
        else:
            age = f"{h // 24}d ago"
        print(f"{s['project_name']:<20} {s['git_branch']:<28} {s['message_count']:>5}  {age}")


if __name__ == "__main__":
    main()
