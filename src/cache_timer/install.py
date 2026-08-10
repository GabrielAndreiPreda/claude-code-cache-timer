"""Install and remove the cache timer as your Claude Code status line.

Writes a ``statusLine`` entry into ~/.claude/settings.json, backing the file up
first, and leaves every other setting alone.

The only command it writes is the bare console script name. That form has no path
separators, no spaces and no characters any shell treats specially, so it does
not matter whether Claude Code routes the status line through Git Bash,
PowerShell or sh, and none of them has to be identified first. If the name is not
on PATH the install stops and says how to fix the PATH, rather than writing an
absolute path that would need quoting for a shell it cannot inspect.

The command is still run against a synthetic payload before it is written,
because a status line that fails renders as a blank row with nothing on stderr to
explain it.
"""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time

CONSOLE_SCRIPT = "claude-cache-timer"

CLAUDE_DIR = os.path.join(os.path.expanduser("~"), ".claude")
SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")
BACKUP_DIR = os.path.join(CLAUDE_DIR, "backups")


def is_ours(command):
    """Whether a statusLine command is one this project installed.

    A substring test rather than equality, because the command may carry flags.
    """
    return CONSOLE_SCRIPT in str(command)


# --------------------------------------------------------------------------
# Choosing the command
# --------------------------------------------------------------------------


class Candidate:
    """The status line invocation, and how to prove it works.

    `tokens` are what settings.json will hold; `argv` is the same thing as a
    directly executable argument list. They differ on purpose: settings.json gets
    the bare console script name, while verification runs the absolute path that
    `shutil.which` resolved it to.
    """

    def __init__(self, tokens, argv):
        self.tokens = tokens
        self.argv = argv

    def command(self):
        """The string to write into settings.json."""
        return " ".join(self.tokens)


def candidate(ascii_only=False):
    """The command to install, or None when it is not on PATH."""
    on_path = shutil.which(CONSOLE_SCRIPT)
    if not on_path:
        return None
    flag = ["--ascii"] if ascii_only else []
    return Candidate([CONSOLE_SCRIPT] + flag, [os.path.abspath(on_path)] + flag)


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def make_fixture(directory):
    """Write a transcript timestamped now, with a known 1h cache write."""
    path = os.path.join(directory, "fixture.jsonl")
    now = time.time()
    record = {
        "timestamp": "%s.%03dZ"
        % (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)), (now % 1) * 1000),
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


def verify(chosen):
    """Run a candidate against a synthetic payload; return (ok, detail).

    Executed directly, never through a shell. The command is a bare name with no
    quoting in it, so there is nothing shell-specific left to get wrong, and
    `shutil.which` having found it is what proves a shell will find it too.
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
        try:
            result = subprocess.run(
                chosen.argv,
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                # The status line forces its own stdout to UTF-8 so the glyphs
                # survive, so this end has to decode UTF-8 to match. Saying only
                # `text=True` decodes with the system's preferred encoding, which
                # off a UTF-8 locale cannot represent the hourglass: on Windows
                # that raises inside a reader thread, where it neither propagates
                # nor fills the buffer, so verification sees empty output and
                # reports a working command as broken.
                encoding="utf-8",
                errors="replace",
            )
        except Exception as error:
            return False, "could not run %s: %s" % (chosen.argv[0], error)
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


def choose(ascii_only=False):
    """The verified command to install, or raise explaining why there is none."""
    chosen = candidate(ascii_only)
    if chosen is None:
        raise SystemExit(
            "\nThe %s command is not on PATH.\n\n"
            "The install put it somewhere your PATH does not cover. Run\n"
            "  uv tool update-shell     (or: pipx ensurepath)\n"
            "open a new terminal, and run this again.\n\n"
            "Nothing was written." % CONSOLE_SCRIPT
        )

    ok, detail = verify(chosen)
    if not ok:
        raise SystemExit(
            "\nThe status line command did not render:\n"
            "  %s\n"
            "  %s\n\n"
            "Nothing was written. Please report this along with the output above."
            % (chosen.command(), detail)
        )
    return chosen, detail


# --------------------------------------------------------------------------
# settings.json
# --------------------------------------------------------------------------


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
    stem = os.path.join(BACKUP_DIR, "settings.json.cache-timer-%d" % int(time.time()))
    target = stem
    attempt = 1
    while os.path.exists(target):
        target = "%s.%d" % (stem, attempt)
        attempt += 1
    shutil.copy2(SETTINGS, target)
    return target


def write_settings(data):
    """Replace settings.json atomically so a crash cannot truncate it."""
    directory = os.path.dirname(SETTINGS)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    handle, temp = tempfile.mkstemp(dir=directory, prefix=".cache-timer-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temp, SETTINGS)
    except Exception:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def install(argv=None):
    parser = argparse.ArgumentParser(
        prog="claude-cache-timer install",
        description="Set the cache timer as your Claude Code status line.",
    )
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
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing status line that is not this one",
    )
    args = parser.parse_args(argv)

    if args.interval < 1:
        raise SystemExit("--interval must be at least 1.")

    chosen, rendered = choose(args.ascii)
    command = chosen.command()
    print("command: %s" % command)
    print("renders: %s" % rendered)

    status_line = {
        "type": "command",
        "command": command,
        "refreshInterval": args.interval,
    }
    try:
        settings = load_settings()
    except UnreadableSettings as error:
        raise SystemExit(manual_instructions(error, status_line))
    existing = settings.get("statusLine")

    # Whatever padding was in effect stays in effect, whether replacing someone
    # else's status line or reinstalling over our own.
    if isinstance(existing, dict) and "padding" in existing:
        status_line["padding"] = existing["padding"]

    foreign = (
        isinstance(existing, dict)
        and existing.get("command")
        and not is_ours(existing.get("command"))
    )
    if foreign and not args.force:
        raise SystemExit(
            "\nYou already have a status line configured:\n"
            "  %s\n\n"
            "Only one can be active at a time. Re-run with --force to replace it;\n"
            "your settings.json is backed up to %s first, so you can put it back."
            % (existing.get("command"), BACKUP_DIR)
        )

    if args.dry_run:
        print("\n--dry-run, not writing. Would set statusLine to:")
        print(json.dumps(status_line, indent=2))
        if foreign:
            print("Would replace: %s" % existing.get("command"))
        return 0

    backup = backup_settings()
    if backup:
        print("\nbacked up settings.json -> %s" % backup)
    if foreign:
        print("replaced your previous status line: %s" % existing.get("command"))

    settings["statusLine"] = status_line
    write_settings(settings)
    print("\nInstalled. Open a new Claude Code session to see it.")
    print("Uninstall with: %s uninstall" % CONSOLE_SCRIPT)
    return 0


def uninstall(argv=None):
    parser = argparse.ArgumentParser(
        prog="claude-cache-timer uninstall",
        description="Remove the cache timer status line from settings.json.",
    )
    parser.parse_args(argv)

    if not os.path.exists(SETTINGS):
        print("No %s; nothing to do." % SETTINGS)
        return 0

    try:
        settings = load_settings()
    except UnreadableSettings as error:
        raise SystemExit(manual_instructions(error))

    existing = settings.get("statusLine")
    if not (isinstance(existing, dict) and is_ours(existing.get("command", ""))):
        print("The cache timer status line is not installed; nothing to do.")
        return 0

    backup = backup_settings()
    if backup:
        print("backed up settings.json -> %s" % backup)

    settings.pop("statusLine", None)
    write_settings(settings)
    print("removed the statusLine entry")
    print("Done. The package itself is still installed; remove it with")
    print("  pipx uninstall claude-code-cache-timer   (or: uv tool uninstall ...)")
    return 0
