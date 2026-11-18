"""Approve explicit filesystem operations whose operands stay in WORKDIR.

This recognizes a subset of shell syntax; it is not a sandbox for scripts.
"""
import glob
import os
import re
import shlex
from pathlib import Path


def inside_workspace(value, cwd, root):
    if not value or value.startswith("~") or re.search(r"[$`\n\r]", value):
        return False
    # Reject provider paths, alternate data streams and foreign drive syntax.
    if ":" in value and not (os.name == "nt" and re.match(r"^[A-Za-z]:[/\\]", value) and value.count(":") == 1):
        return False
    if os.name != "nt" and "\\" in value:
        return False
    try:
        path = cwd / value
        if not path.resolve().is_relative_to(root):
            return False
        if glob.has_magic(str(path)):
            # Check actual wildcard matches, including symlinks/junctions.
            return all(Path(match).resolve().is_relative_to(root)
                       for match in glob.iglob(str(path), recursive=True))
        return True
    except (OSError, ValueError):
        return False


def trusted_command(command, root, cwd):
    if not cwd.is_relative_to(root):
        return "ask"
    if re.search(r"[$`(){}\n\r#]", command):
        return "ask"
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        lexer.escape = ""  # Preserve native Windows paths; do not parse escapes.
        tokens = list(lexer)
    except ValueError:
        return "ask"
    segments, separators, words, redirects = [], [], [], []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {">", ">>", "<"}:
            if i + 1 == len(tokens):
                return "ask"
            redirects.append(tokens[i + 1])
            if words and words[-1] in {"0", "1", "2"}:
                words.pop()
            i += 2
            continue
        if token in {"&&", ";", "|"}:
            if not words:
                return "ask"
            segments.append((words, redirects))
            separators.append(token)
            words, redirects = [], []
        elif any(char in token for char in ";&|<>"):
            return "ask"
        else:
            words.append(token)
        i += 1
    if not words:
        return "ask"
    segments.append((words, redirects))
    for index, (words, redirects) in enumerate(segments):
        if any(target != "/dev/null" and not inside_workspace(target, cwd, root) for target in redirects):
            return "ask"
        name, args = words[0].lower(), words[1:]
        # Pipeline text may name arbitrary paths. Do not feed it into destructive
        # PowerShell commands even when the producer only reads a local file.
        if index and separators[index - 1] == "|" and name not in {
                "cat", "head", "tail", "wc", "rg", "grep", "echo", "out-file"}:
            return "ask"
        if name in {"cd", "set-location"}:
            if args[:1] == ["--"] or args[:1] in (["-Path"], ["-LiteralPath"]):
                args = args[1:]
            if (len(args) != 1 or not inside_workspace(args[0], cwd, root)
                    or (index and separators[index - 1] == "|")
                    or (index < len(separators) and separators[index] != "&&")):
                return "ask"
            cwd = (cwd / args[0]).resolve()
            continue
        if not scoped_operation(name, args, cwd, root):
            return "ask"
    return "allow"


def scoped_operation(name, args, cwd, root):
    if name in {"echo", "pwd", "whoami", "get-location"}:
        return True
    if name in {"rg", "grep"}:
        pattern_seen = False
        paths = []
        for arg in args:
            if arg.startswith("-"):
                if arg not in {"-n", "-i", "-r", "-R", "-l", "-q", "--files", "--hidden", "--no-ignore"}:
                    return False
                if arg == "--files":
                    pattern_seen = True
            elif not pattern_seen:
                pattern_seen = True
            else:
                paths.append(arg)
        return all(inside_workspace(path, cwd, root) for path in paths)
    # Only local Git operations; remote/system configuration still needs approval.
    if name == "git":
        if len(args) >= 2 and args[0] == "-C":
            if not inside_workspace(args[1], cwd, root):
                return False
            cwd, args = (cwd / args[1]).resolve(), args[2:]
        if not args or args[0] not in {"status", "diff", "log", "show", "rev-parse", "ls-files",
                                       "add", "commit", "reset", "clean", "restore", "checkout", "rm", "mv"}:
            return False
        # Options capable of executing another program or writing implicit paths.
        if any(arg.split("=", 1)[0] in {"--exec", "--ext-diff", "--textconv", "--output", "--git-dir", "--work-tree"}
               for arg in args):
            return False
        args = args[1:]
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in {"-m", "--message"}:
                i += 2
                continue
            if not arg.startswith("-") and not inside_workspace(arg, cwd, root):
                return False
            # Do not accept file-valued flags such as commit -F outside.
            if arg in {"-F", "--file"}:
                if i + 1 >= len(args) or not inside_workspace(args[i + 1], cwd, root):
                    return False
                i += 1
            elif arg.startswith("-F") and len(arg) > 2:
                if not inside_workspace(arg[2:], cwd, root):
                    return False
            elif "=" in arg and not inside_workspace(arg.split("=", 1)[1], cwd, root):
                return False
            i += 1
        return True
    if name in {"pytest", "python", "python3", "npm"}:
        # Keep the existing trusted test/build allowances, not arbitrary code -c.
        if name.startswith("python"):
            if args[:2] not in (["-m", "pytest"], ["-m", "unittest"], ["-m", "compileall"]):
                return False
            args = args[2:]
        elif name == "npm":
            if args[:1] == ["test"]:
                args = args[1:]
            elif len(args) >= 2 and args[:1] == ["run"] and args[1] in {"test", "build", "lint"}:
                args = args[2:]
            else:
                return False
        return all(inside_workspace(arg.split("=", 1)[-1], cwd, root) for arg in args)
    file_commands = {"rm", "rmdir", "unlink", "del", "erase", "rd", "mkdir", "touch", "cp", "mv", "copy", "move",
                     "cat", "head", "tail", "ls", "dir", "wc", "chmod", "chown", "truncate",
                     "remove-item", "new-item", "copy-item", "move-item", "rename-item", "get-content",
                     "set-content", "add-content", "clear-content", "out-file", "get-childitem"}
    if name not in file_commands:
        return False
    # Flags are explicitly enumerated: unknown flags might execute code, follow
    # links, or carry hidden file targets. Path options leave their operand intact.
    switches = {"--", "--recursive", "--force", "--verbose", "--parents", "--preserve-root",
                "--no-preserve-root", "-recurse", "-force", "-literalpath", "-path", "-destination",
                "-newname", "-filepath", "-append", "-n", "-c", "--lines", "--bytes"}
    value_flags = ({"-value", "-itemtype", "-encoding", "-erroraction", "-filter", "-include", "-exclude"}
                   if "-" in name else set())
    i = 0
    operands = []
    end_options = False
    while i < len(args):
        arg, low = args[i], args[i].lower()
        if end_options:
            operands.append(arg)
            i += 1
            continue
        if arg == "--":
            end_options = True
            i += 1
            continue
        if os.name == "nt" and name in {"del", "erase", "rd", "rmdir"} and low in {"/s", "/q", "/f"}:
            i += 1
            continue
        if low in value_flags:
            if i + 1 >= len(args):
                return False
            if low == "-itemtype" and args[i + 1].lower() not in {"file", "directory"}:
                return False
            i += 2
            continue
        if low in switches or re.fullmatch(r"-[rfvpdqalshnci]+|-[0-9]+", low):
            i += 1
            continue
        if arg.startswith("-"):
            return False
        operands.append(arg)
        i += 1
    if name in {"chmod", "chown"}:
        operands = operands[1:]
    if name == "out-file" and not operands:
        return False
    return all(inside_workspace(arg, cwd, root) for arg in operands)
