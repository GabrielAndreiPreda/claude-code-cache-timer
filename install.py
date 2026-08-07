#!/usr/bin/env python3
"""Install the cache timer as your Claude Code status line.

Writes a ``statusLine`` entry into ~/.claude/settings.json, backing the file up
first. If you already have a status line, this keeps it and wraps it: your command
still runs, and the cache segment lands at the end of its output.

Quoting differs between the shells Claude Code may route through, so this runs the
command it generates against a synthetic payload and refuses to touch settings.json
unless it renders. A quoting mistake otherwise fails silently and leaves a blank row.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "cache_timer_statusline.py")

CLAUDE_DIR = os.path.join(os.path.expanduser("~"), ".claude")
SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")
BACKUP_DIR = os.path.join(CLAUDE_DIR, "backups")
STATE_DIR = os.path.join(CLAUDE_DIR, "cache-timer")
WRAPPED = os.path.join(STATE_DIR, "wrapped.json")

# Substring identifying a statusLine command as ours, used to detect reinstalls.
MARKER = "cache_timer_statusline.py"

IS_WINDOWS = platform.system() == "Windows"


def forward(path):
    """Windows paths must use forward slashes.

    Git Bash treats unquoted backslashes as escapes and silently mangles the
    command. Python accepts forward slashes on Windows, so this is safe.
    """
    return path.replace("\\", "/")


def has_git_bash():
    """Whether Claude Code will route the status line through Git Bash.

    On Windows it uses Git Bash when that is installed, PowerShell when it is not.
    """
    if not IS_WINDOWS:
        return True
    return shutil.which("bash") is not None


def resolve_interpreter():
    """Pick the Python that should run the status line script.

    Returns a list of command tokens. Baking an absolute path handles distros
    where python3 is somewhere unusual, and avoids Windows, where ``python3`` is
    a Microsoft Store alias stub rather than a real interpreter.
    """
    if IS_WINDOWS and shutil.which("py"):
        # The launcher survives Python upgrades, unlike a versioned path.
        return ["py", "-3"]

    executable = sys.executable
    if not executable:
        raise SystemExit(
            "Could not determine the running Python interpreter.\n"
            "Re-run this installer with an explicit interpreter, e.g.\n"
            "    python3 install.py"
        )

    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        # A venv path would break the status line the moment the venv is removed.
        base = os.path.join(
            getattr(sys, "base_prefix"),
            "Scripts" if IS_WINDOWS else "bin",
            "python.exe" if IS_WINDOWS else "python3",
        )
        if os.path.exists(base):
            print(
                "note: running inside a virtualenv; baking the base interpreter\n"
                "      %s\n"
                "      so the status line survives the venv being removed." % base
            )
            return [base]
        print(
            "warning: running inside a virtualenv. The status line will stop\n"
            "         working if %s is removed. Re-run install.py with your\n"
            "         system Python to avoid this." % sys.prefix
        )
    return [executable]


def is_path(token):
    """Whether a command token is a filesystem path, not a bare name or a flag."""
    return not token.startswith("-") and ("/" in token or "\\" in token)


def quote(token):
    """Quote a token if it is a path; leave bare flags and names alone."""
    return '"%s"' % forward(token) if is_path(token) else token


def build_command(interpreter, script, ascii_only=False, powershell=False):
    """Assemble the statusLine command string for the target shell."""
    tokens = [quote(part) for part in interpreter] + [quote(script)]
    if ascii_only:
        tokens.append("--ascii")
    command = " ".join(tokens)
    # PowerShell needs the call operator to execute a quoted path. It is a syntax
    # error in bash, so it is added only when PowerShell will run the command and
    # the interpreter is a path rather than a bare name like `py`.
    if powershell and is_path(interpreter[0]):
        command = "& " + command
    return command


def make_fixture(directory):
    """Write a transcript with a fresh mtime and a known 1h cache write."""
    path = os.path.join(directory, "fixture.jsonl")
    record = {
        "type": "assistant",
        "message": {
            "usage": {
                "cache_creation": {
                    "ephemeral_1h_input_tokens": 1234,
                    "ephemeral_5m_input_tokens": 0,
                }
            }
        },
    }
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return path


def shell_for_verification():
    """The shell Claude Code would use, as an argv prefix."""
    if IS_WINDOWS:
        if has_git_bash():
            return ["bash", "-c"]
        return ["powershell", "-NoProfile", "-Command"]
    return ["bash", "-c"] if shutil.which("bash") else ["sh", "-c"]


def verify(command):
    """Run the command through the real shell and confirm it renders.

    Returns (ok, detail). This catches quoting mistakes, which otherwise fail
    silently and leave a blank status line.
    """
    directory = tempfile.mkdtemp(prefix="cache-timer-verify-")
    try:
        fixture = make_fixture(directory)
        # Carries a model so the check exercises the context prefix too, but no
        # directory: the only one available here is a temp path, and echoing it
        # back at the user would read as a mistake.
        payload = json.dumps(
            {
                "transcript_path": fixture,
                "session_id": "verify",
                "model": {"display_name": "Opus"},
            }
        )
        argv = shell_for_verification() + [command]
        try:
            result = subprocess.run(
                argv,
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                universal_newlines=True,
            )
        except Exception as error:
            return False, "could not run %s: %s" % (argv[0], error)
        if result.returncode != 0:
            return False, "exit %d; stderr: %s" % (
                result.returncode,
                (result.stderr or "").strip()[:400],
            )
        output = (result.stdout or "").strip()
        if not output:
            return False, "produced no output"
        if not any(char.isdigit() for char in output):
            return False, "output has no countdown: %r" % output[:200]
        return True, output
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class UnreadableSettings(Exception):
    """settings.json exists but this script will not risk rewriting it."""


def load_settings():
    """Parse settings.json, or refuse rather than risk mangling it.

    Claude Code accepts comments and trailing commas in settings.json. Python's
    json module does not. Stripping them to make the parse succeed would delete
    the user's comments on the way back out, so an unparseable file is a hard
    stop with instructions instead.
    """
    if not os.path.exists(SETTINGS):
        return {}
    with open(SETTINGS, "r", encoding="utf-8") as handle:
        text = handle.read()
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as error:
        raise UnreadableSettings(str(error))
    if not isinstance(data, dict):
        raise UnreadableSettings("the file does not contain a JSON object")
    return data


def manual_instructions(error, status_line=None):
    """Text telling the user how to make the change by hand."""
    lines = [
        "",
        "Could not parse %s" % SETTINGS,
        "  %s" % error,
        "",
        "Claude Code allows comments and trailing commas here; this script does",
        "not, and rewriting the file would strip them. Nothing was changed.",
    ]
    if status_line is not None:
        body = json.dumps(status_line, indent=2).replace("\n", "\n  ")
        lines += [
            "",
            "To install by hand, add this key to the top level of that file:",
            "",
            '  "statusLine": %s' % body,
        ]
    else:
        lines += ["", "To uninstall by hand, remove the top-level statusLine key."]
    return "\n".join(lines)


def backup_settings():
    if not os.path.exists(SETTINGS):
        return None
    if not os.path.isdir(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)
    # Second resolution alone is not enough: installing and then uninstalling
    # within the same second would otherwise reuse the name and destroy the
    # pre-install copy, which is the one worth keeping.
    stem = os.path.join(
        BACKUP_DIR, "settings.json.cache-timer-%d" % int(time.time())
    )
    target = stem
    attempt = 1
    while os.path.exists(target):
        target = "%s.%d" % (stem, attempt)
        attempt += 1
    shutil.copy2(SETTINGS, target)
    return target


def write_json(path, data):
    """Replace `path` with `data` atomically so a crash cannot truncate it.

    Creates the parent directory if it is missing.
    """
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    handle, temp = tempfile.mkstemp(dir=directory, prefix=".cache-timer-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temp, path)
    except Exception:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


def write_settings(data):
    write_json(SETTINGS, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="use ASCII instead of emoji, for terminals that render them badly",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=1,
        help="seconds between refreshes (default 1, minimum 1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="verify and print the change without writing settings.json",
    )
    args = parser.parse_args()

    if args.interval < 1:
        raise SystemExit("--interval must be at least 1.")
    if not os.path.exists(SCRIPT):
        raise SystemExit("Cannot find %s next to this installer." % SCRIPT)

    powershell = IS_WINDOWS and not has_git_bash()
    interpreter = resolve_interpreter()
    command = build_command(
        interpreter, SCRIPT, ascii_only=args.ascii, powershell=powershell
    )

    print("shell:   %s" % ("PowerShell" if powershell else "bash"))
    print("command: %s" % command)

    ok, detail = verify(command)
    if not ok:
        raise SystemExit(
            "\nThe generated command did not work: %s\n"
            "Nothing was written. Please report this along with the command "
            "above." % detail
        )
    print("verified, renders: %s" % detail)

    status_line = {"type": "command", "command": command, "refreshInterval": args.interval}
    try:
        settings = load_settings()
    except UnreadableSettings as error:
        raise SystemExit(manual_instructions(error, status_line))
    existing = settings.get("statusLine")

    # Whatever padding was in effect stays in effect, whether we are wrapping
    # someone else's status line or reinstalling over our own.
    if isinstance(existing, dict) and "padding" in existing:
        status_line["padding"] = existing["padding"]

    # Only wrap a status line that is not already ours; reinstalling must leave
    # any previously wrapped command untouched.
    wrap = None
    if isinstance(existing, dict) and MARKER not in str(existing.get("command", "")):
        wrap = existing

    if args.dry_run:
        print("\n--dry-run, not writing. Would set statusLine to:")
        print(json.dumps(status_line, indent=2))
        if wrap:
            print("Would wrap existing command: %s" % wrap.get("command"))
        return 0

    backup = backup_settings()
    if backup:
        print("backed up settings.json -> %s" % backup)

    if wrap:
        write_json(WRAPPED, wrap)
        print("wrapping your existing status line: %s" % wrap.get("command"))
        print("saved to %s" % WRAPPED)

    settings["statusLine"] = status_line
    write_settings(settings)
    print("\nInstalled. Open a new Claude Code session to see it.")
    print("Uninstall with: %s uninstall.py" % " ".join(interpreter))
    return 0


if __name__ == "__main__":
    sys.exit(main())
