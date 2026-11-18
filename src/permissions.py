"""Conservative command classification, shared by workers and goal checks.

This is an approval policy, not an operating-system sandbox.
"""
import json
import os
import re
import shlex
import threading
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar

import src.mcp as mcp

PERMISSION_CWD = ContextVar("permission_cwd", default=None)


@contextmanager
def permission_context(cwd):
    token = PERMISSION_CWD.set(cwd)
    try:
        yield
    finally:
        PERMISSION_CWD.reset(token)


class PermissionPolicy:
    def __init__(self, mode=None, workspace=None, cwd=None):
        self.mode = mode or os.getenv("AGENT_PERMISSION_MODE", "strict")
        self.workspace = Path(workspace or os.getenv("WORKDIR") or Path(__file__).resolve().parents[1]).resolve()
        self.cwd = Path(cwd or PERMISSION_CWD.get() or self.workspace).resolve()
        if self.mode not in {"strict", "trusted_workspace", "unattended"}:
            raise ValueError("Invalid AGENT_PERMISSION_MODE")

    def classify(self, command):
        if not isinstance(command, str) or not command.strip():
            return "deny"
        value = command.lower().strip()
        if re.search(r"\b(sudo|shutdown|reboot|mkfs|diskpart|format-volume)\b|\bdd\s+if=|rm\s+.*\s/(?:\s|$)", value):
            return "deny"
        if self.mode == "trusted_workspace":
            from src.workspace_permissions import trusted_command
            return trusted_command(command, self.workspace, self.cwd)
        # Recognize a deliberately small read-only shell grammar before applying
        # the fallback policy. Never grant an entire chain based on its prefix.
        if self._readonly_chain(command):
            return "allow"
        # Unsupported shell syntax, interpreters and unknown commands need approval.
        if re.search(r"[;&|><`\n\r]|\$\(|\b(rm|del|erase|remove-item|rmdir|chmod|chown|invoke-expression|iex)\b", value):
            return "ask"
        if re.search(r"\bgit\s+(reset|clean|push|checkout|restore|rebase)\b", value):
            return "ask"
        if re.match(r"^(pwd|ls|dir|rg|grep|cat|head|tail|wc|whoami|echo|get-location|get-childitem|get-content)(\s|$)", value):
            return "allow"
        if re.match(r"^git\s+(status|diff|log|show|rev-parse|ls-files)(\s|$)", value):
            return "allow"
        if re.match(r"^(pytest|python(?:3)?\s+-m\s+(pytest|unittest|compileall))(\s|$)", value):
            return "allow"
        return "ask"

    def _readonly_chain(self, command):
        # Expansion/escaping differs between POSIX shells and PowerShell. Leave
        # these forms to interactive approval rather than pretending to parse them.
        if re.search(r"[$`\\(){}\n\r#]", command):
            return False
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            return False
        segments, connectors, words = [], [], []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token == ">":
                # Only discard output; never auto-approve writing a real file,
                # fd duplication, append redirection or input redirection.
                if index + 1 >= len(tokens) or tokens[index + 1] != "/dev/null":
                    return False
                if words and words[-1] in {"1", "2"}:
                    words.pop()
                index += 2
                continue
            if token in {"&&", "|"}:
                if not words:
                    return False
                segments.append(words)
                connectors.append(token)
                words = []
            elif any(char in token for char in ";&|<>"):
                return False
            else:
                words.append(token)
            index += 1
        if not words:
            return False
        segments.append(words)
        cwd = self.cwd
        for index, words in enumerate(segments):
            name, args = words[0].lower(), words[1:]
            if name == "cd":
                if args[:1] == ["--"]:
                    args = args[1:]
                if len(args) != 1 or args[0].startswith(("-", "~")):
                    return False
                # cd in a pipeline has shell-specific scope; accept only &&.
                if (index and connectors[index - 1] == "|") or (index < len(connectors) and connectors[index] == "|"):
                    return False
                try:
                    cwd = (cwd / args[0]).resolve()
                    if not cwd.is_relative_to(self.workspace):
                        return False
                except (OSError, ValueError):
                    return False
            elif name == "git":
                if not args or args[0] not in {"status", "diff", "log", "show", "rev-parse", "ls-files"}:
                    return False
                if any(arg.split("=", 1)[0] in {"--output", "--ext-diff", "--textconv"} for arg in args):
                    return False
            elif name in {"rg", "grep"}:
                if any(arg.split("=", 1)[0] in {"--pre", "--hostname-bin"} for arg in args):
                    return False
            elif name not in {"pwd", "ls", "dir", "cat", "head", "tail", "wc", "whoami", "echo",
                              "get-location", "get-childitem", "get-content"}:
                return False
        return True

    def check(self, command, ask=None):
        action = self.classify(command)
        if action == "allow":
            return None
        if action == "deny":
            return "Permission denied: command is blocked by policy"
        if self.mode == "unattended" or threading.current_thread() is not threading.main_thread() or ask is None:
            return "Permission denied: risky operation requires interactive mode"
        return None if ask(command) else "Permission denied by user"


# -- Tool authorization --
#
# Tool authorization is applied by the dispatchers before a handler runs.  It
# is intentionally not an event/callback system: each caller asks a single
# question and gets either ``None`` (allowed) or a denial reason.

_approval_cache: dict[str, bool] = {}
_approval_lock = threading.Lock()


def _remember_approval(key: str, allowed: bool) -> None:
    with _approval_lock:
        _approval_cache[key] = allowed


def _cached_approval(key: str) -> bool | None:
    with _approval_lock:
        return _approval_cache.get(key)


def _approve_once(key: str, reason: str) -> bool:
    """Ask once per exact approval key, then remember the answer."""
    cached = _cached_approval(key)
    if cached is not None:
        return cached
    allowed = _ask_user(reason)
    _remember_approval(key, allowed)
    return allowed


def _ask_user(reason: str) -> bool:
    if (
        PermissionPolicy().mode == "unattended"
        or threading.current_thread() is not threading.main_thread()
    ):
        return False
    from src.config import CONSOLE, terminal_print

    terminal_print(f"\n\033[33m[permission] {reason}\033[0m")
    choice = CONSOLE.ask("  Allow? [y/N] ").strip().lower()
    return choice in {"y", "yes"}


def _tool_name(block) -> str:
    if isinstance(block, dict):
        return str(block.get("name", ""))
    return str(getattr(block, "name", ""))


def _tool_input(block) -> dict:
    if isinstance(block, dict):
        value = block.get("input", {})
    else:
        value = getattr(block, "input", {})
    return value if isinstance(value, dict) else {}


def authorize_tool(block) -> str | None:
    """Return a denial reason, or ``None`` when the tool may run."""
    # Imported lazily so importing the permission module does not freeze the
    # workspace before the caller has applied its environment configuration.
    from src.config import WORKDIR

    name = _tool_name(block)
    tool_input = _tool_input(block)

    if name == "bash":
        command = tool_input.get("command", "")
        if not isinstance(command, str):
            return "Permission denied: shell command must be a string"
        cwd = str(PERMISSION_CWD.get() or WORKDIR)
        approval_key = f"bash:{cwd}:{command}"
        denied = PermissionPolicy(workspace=WORKDIR).check(
            command,
            lambda value: _approve_once(
                approval_key, f"Allow shell command {value!r}?"
            ),
        )
        if denied:
            return denied

    if name in ("read_file", "write_file", "edit_file"):
        path = tool_input.get("path", "")
        if not isinstance(path, str):
            return "Permission denied: path must be a string"
        base = PERMISSION_CWD.get() or WORKDIR
        if not (base / path).resolve().is_relative_to(WORKDIR):
            return "Permission denied: path is outside the workspace"

    if name.startswith("mcp__"):
        policy = mcp.mcp_tool_policies.get(name, "confirm")
        if policy == "deny":
            return "Permission denied: MCP tool blocked by policy"
        if policy != "allow":
            if (
                PermissionPolicy().mode == "unattended"
                or threading.current_thread() is not threading.main_thread()
            ):
                return (
                    "Permission denied: interactive MCP approval is unavailable "
                    "during an asynchronous turn"
                )
            approval_key = (
                f"mcp:{name}:"
                f"{json.dumps(tool_input, ensure_ascii=False, sort_keys=True, default=str)}"
            )
            if not _approve_once(approval_key, f"MCP tool: {name}"):
                return "Permission denied by user"
    return None
