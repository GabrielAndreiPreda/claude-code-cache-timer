#!/usr/bin/env python3
"""Claude Code status line segment showing time left on the session's prompt cache.

Reads the status line JSON payload on stdin, finds the session transcript, and
prints how long the prompt cache has before it expires. Configuring a status line
replaces Claude Code's built-in one, so the model name is redrawn here too unless
a wrapped command is already drawing the row.

The clock is the transcript's mtime: Claude Code appends to the file on every
message, so it advances on every API call the parent session makes. Subagents write
to a ``subagents/`` subdirectory instead, so the parent mtime correctly stalls while
a subagent runs. The cache really is draining during that time.

The TTL is read from the transcript rather than assumed. Sessions run on either the
5-minute or the 1-hour cache, and can move between them, so a hardcoded value is
wrong by a factor of 12 half the time.

Nothing here may raise or write to stderr. This runs once a second and a crash
blanks the status line row.
"""

import io
import json
import math
import os
import subprocess
import sys
import time

# Colours are ANSI SGR codes. Claude Code captures our stdout and renders it, so
# these survive into the terminal.
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"

TTL_1H = 3600
TTL_5M = 300

# How far back to read looking for a cache_creation record. A single transcript
# line can be large (a 75 KiB record is normal when a tool returns a lot), so the
# window has to hold several of them.
TAIL_WINDOW = 256 * 1024
TAIL_WINDOW_WIDE = 2 * 1024 * 1024

WRAPPED_CONFIG = os.path.join(
    os.path.expanduser("~"), ".claude", "cache-timer", "wrapped.json"
)
WRAPPED_TIMEOUT = 2.0


def _force_utf8_stdout():
    """Make stdout UTF-8 so the glyphs survive.

    Windows Python defaults stdout to the ANSI code page (cp1252), which cannot
    encode the hourglass or snowflake and would raise on every tick.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        # Python 3.6 and earlier have no reconfigure(). Rewrap the buffer.
        try:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace"
            )
        except Exception:
            pass
    except Exception:
        pass


def read_tail(path, size, window):
    """Return complete lines from the last `window` bytes of a file.

    `size` is passed in rather than stat'ed here: the caller has already stat'ed
    the transcript, and this runs once a second.

    Drops the first line, which is usually the tail of a record that started
    before the window opened.
    """
    with open(path, "rb") as handle:
        if size > window:
            handle.seek(size - window)
        data = handle.read()
    if size > window:
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline != -1 else b""
    return [line for line in data.split(b"\n") if line.strip()]


def ttl_from_lines(lines):
    """Find the TTL of the most recent cache write, or None.

    Walks backwards for the newest ``cache_creation`` with a non-zero bucket. A
    turn that only reads the cache records ``{0, 0}``, so skip those rather than
    treating them as an answer.
    """
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        created = usage.get("cache_creation")
        if not isinstance(created, dict):
            continue
        if created.get("ephemeral_1h_input_tokens"):
            return TTL_1H
        if created.get("ephemeral_5m_input_tokens"):
            return TTL_5M
    return None


def find_ttl(path, size=None):
    """Read the transcript tail and determine the cache TTL in seconds."""
    if size is None:
        size = os.path.getsize(path)
    ttl = ttl_from_lines(read_tail(path, size, TAIL_WINDOW))
    if ttl is not None:
        return ttl
    # The window may have landed inside one enormous record. Try once more with a
    # wider one before giving up.
    if size > TAIL_WINDOW:
        ttl = ttl_from_lines(read_tail(path, size, TAIL_WINDOW_WIDE))
    return ttl


def format_clock(seconds):
    """Render seconds as M:SS, or H:MM:SS past an hour.

    Rounds up: with 41.2 seconds left the display reads 0:42 and ticks down to
    0:01 before going cold, rather than truncating to 0:41 and showing 0:00 for a
    whole second while the cache is still alive.
    """
    seconds = int(math.ceil(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


def render(path, ascii_only=False):
    """Build the status line segment for a transcript, or None if unavailable."""
    hourglass = "~" if ascii_only else "⏳"
    snowflake = "*" if ascii_only else "❄️"
    separator = "-" if ascii_only else "·"

    # A payload that is missing the key, or carries a non-string, is not an
    # error worth reporting. Note that os.stat() accepts an integer as a file
    # descriptor, so an unchecked number here would stat something unrelated.
    if not path or not isinstance(path, str):
        return None
    # Resolved fresh every tick: the transcript does not exist yet at session
    # start, and compaction replaces it.
    try:
        info = os.stat(path)
    except OSError:
        return None
    age = time.time() - info.st_mtime

    ttl = find_ttl(path, info.st_size)
    if ttl is None:
        # No cache write on record. If the transcript is older than the longest
        # TTL there is, it is cold whichever bucket it used.
        if age > TTL_1H:
            return "%s%s cold%s" % (DIM, snowflake, RESET)
        return "%scache ?%s" % (DIM, RESET)

    label = "1h" if ttl == TTL_1H else "5m"
    remaining = ttl - age
    if remaining <= 0:
        return "%s%s cold%s" % (DIM, snowflake, RESET)

    fraction = remaining / float(ttl)
    if fraction > 0.5:
        colour = GREEN
    elif fraction > 0.2:
        colour = YELLOW
    else:
        colour = RED
    return "%s%s %s%s %s%s %s%s" % (
        colour,
        hourglass,
        format_clock(remaining),
        RESET,
        DIM,
        separator,
        label,
        RESET,
    )


def abbreviate_home(path):
    """Render an absolute path with ~ for the home directory."""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def find_git_dir(directory):
    """Locate the .git directory governing `directory`, or None."""
    current = os.path.abspath(directory)
    while True:
        candidate = os.path.join(current, ".git")
        if os.path.isdir(candidate):
            return candidate
        if os.path.isfile(candidate):
            # Linked worktrees and submodules leave a `gitdir: <path>` pointer
            # here instead of a directory.
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    pointer = handle.read().strip()
            except Exception:
                return None
            if not pointer.startswith("gitdir:"):
                return None
            target = pointer[len("gitdir:"):].strip()
            return target if os.path.isabs(target) else os.path.join(current, target)
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def git_branch(directory):
    """The current branch, or a short SHA when HEAD is detached.

    Reads .git/HEAD directly. Spawning `git` once a second would cost more than
    every other thing this script does put together.
    """
    location = find_git_dir(directory)
    if not location:
        return None
    try:
        with open(os.path.join(location, "HEAD"), "r", encoding="utf-8") as handle:
            head = handle.read().strip()
    except Exception:
        return None
    if head.startswith("ref:"):
        ref = head[4:].strip()
        if ref.startswith("refs/heads/"):
            # Sliced rather than split, so `feature/x` survives intact.
            ref = ref[len("refs/heads/"):]
        return ref or None
    return head[:7] or None


def render_context(payload, ascii_only=False):
    """Redraw model, directory and branch, or None if the payload has neither.

    A configured status line replaces Claude Code's built-in one entirely, so
    everything it used to show is gone unless this draws it. Only used when
    nothing else is drawing the row.
    """
    if not isinstance(payload, dict):
        return None

    parts = []
    model = payload.get("model")
    if isinstance(model, dict):
        name = model.get("display_name")
        if isinstance(name, str) and name:
            parts.append(name)

    workspace = payload.get("workspace")
    directory = workspace.get("current_dir") if isinstance(workspace, dict) else None
    if not isinstance(directory, str) or not directory:
        directory = payload.get("cwd")
    if isinstance(directory, str) and directory:
        parts.append(abbreviate_home(directory))
        branch = git_branch(directory)
        if branch:
            parts.append(branch)

    if not parts:
        return None
    separator = " %s " % ("-" if ascii_only else "·")
    return "%s%s%s" % (DIM, separator.join(parts), RESET)


def prefix_context(context, segment):
    """Join the context prefix and the cache segment; either may be absent."""
    if not context:
        return segment
    return "%s  %s" % (context, segment) if segment else context


def load_wrapped():
    """Return the status line command we displaced at install time, if any."""
    try:
        with open(WRAPPED_CONFIG, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except Exception:
        return None
    if isinstance(config, dict):
        command = config.get("command")
        if isinstance(command, str) and command.strip():
            return command
    return None


def run_wrapped(command, payload_text):
    """Run the displaced status line command, feeding it the same payload."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            input=payload_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=WRAPPED_TIMEOUT,
            universal_newlines=True,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or "").rstrip("\n")
    return output if output.strip() else None


def combine(inner, segment):
    """Append our segment to the last line of the wrapped command's output."""
    if inner is None:
        return segment
    if segment is None:
        return inner
    lines = inner.split("\n")
    lines[-1] = "%s  %s" % (lines[-1], segment)
    return "\n".join(lines)


def compose(inner, segment, context):
    """Build the final row from the wrapped output, our segment, and the context.

    The context is drawn only when the wrapped command is absent or produced
    nothing, since a status line that draws this row already shows its own.
    """
    if inner is None:
        return prefix_context(context, segment)
    return combine(inner, segment)


def main():
    _force_utf8_stdout()
    ascii_only = "--ascii" in sys.argv[1:]

    try:
        payload_text = sys.stdin.read()
    except Exception:
        payload_text = ""

    payload = None
    try:
        payload = json.loads(payload_text)
    except Exception:
        pass
    transcript = payload.get("transcript_path") if isinstance(payload, dict) else None

    try:
        segment = render(transcript, ascii_only=ascii_only)
    except Exception:
        segment = None

    try:
        context = render_context(payload, ascii_only=ascii_only)
    except Exception:
        context = None

    inner = None
    wrapped = load_wrapped()
    if wrapped:
        inner = run_wrapped(wrapped, payload_text)

    output = compose(inner, segment, context)
    if output:
        try:
            sys.stdout.write(output + "\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Last line of defence. A traceback on stderr every second would be worse
        # than showing nothing.
        sys.exit(0)
