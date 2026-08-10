"""Claude Code status line segment showing time left on the session's prompt cache.

Reads the status line JSON payload on stdin, finds the session transcript, and
prints how long the prompt cache has before it expires. Configuring a status line
replaces Claude Code's built-in one, so the model name is redrawn here too.

The clock is the timestamp on the last assistant turn carrying a ``usage`` block,
because only an API call produces one. It is deliberately not the last record in the
file: most of what a transcript logs is local, and much of that is timestamped, so a
clock keyed to the newest record of any kind restarts on events that cost nothing.
Running a slash command such as ``/exit`` is the clearest case -- it appends a
timestamped record and sends no request. Subagents write to a ``subagents/``
subdirectory, so the parent's clock correctly stalls while a subagent runs. The cache
really is draining during that time.

The file's own mtime is wrong for the same reason, only more so. Claude Code touches
transcripts long after a session's last API call, and a countdown keyed to mtime
restarts from full every time it does, so a session that has been idle for days can
report most of an hour still on the clock.

The TTL is read from the transcript rather than assumed. Sessions run on either the
5-minute or the 1-hour cache, and can move between them, so a hardcoded value is
wrong by a factor of 12 half the time.

Nothing here may raise or write to stderr. This runs once a second and a crash
blanks the status line row.
"""

import calendar
import json
import math
import os
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


def _force_utf8_stdout():
    """Make stdout UTF-8 so the glyphs survive.

    Windows Python defaults stdout to the ANSI code page (cp1252), which cannot
    encode the hourglass or snowflake and would raise on every tick.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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


def is_api_response(record):
    """Whether a record is proof that an API call happened.

    Only assistant turns carry a ``usage`` block and only a response from the API
    produces one, which makes it the one reliable marker in the file. Everything
    else is written locally and must not set the clock, however recent its
    timestamp looks.

    An allowlist on purpose. A record type added to Claude Code later is ignored
    rather than becoming a new way to reset the countdown, and a marker missed
    here understates the cache rather than overstating it.
    """
    if record.get("type") != "assistant":
        return False
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    return isinstance(message.get("usage"), dict)


def ttl_from_record(record):
    """The cache TTL a record's usage implies, or None.

    A turn that only reads the cache records ``{0, 0}``, which says nothing about
    the bucket, so those have to read as no answer rather than as an answer.
    """
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    created = usage.get("cache_creation")
    if not isinstance(created, dict):
        return None
    if created.get("ephemeral_1h_input_tokens"):
        return TTL_1H
    if created.get("ephemeral_5m_input_tokens"):
        return TTL_5M
    return None


def epoch(stamp):
    """Seconds since the epoch for a transcript timestamp, or None.

    Every record carries one as an ISO 8601 string in UTC, to the millisecond:
    ``2025-01-31T09:15:42.318Z``. The fraction is kept. Dropping it would floor
    the anchor and leave the countdown up to a second pessimistic, which is
    visible on a display that ticks once a second.
    """
    if not isinstance(stamp, str):
        return None
    try:
        seconds = calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None
    fraction = stamp[19:].rstrip("Z")
    if not fraction:
        return seconds
    try:
        return seconds + float(fraction)
    except ValueError:
        # An offset such as +00:00 rather than a fraction. The whole second is
        # close enough to carry on with.
        return seconds


def scan(lines):
    """The cache TTL and the time of the last API call, from a transcript tail.

    One backwards walk for both, since the answers are usually a line or two
    apart and this runs once a second. Either may be None.

    Records that are not API responses are skipped outright rather than being
    allowed to set the clock; see `is_api_response`.
    """
    ttl = None
    written = None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict) or not is_api_response(record):
            continue
        if written is None:
            written = epoch(record.get("timestamp"))
        if ttl is None:
            ttl = ttl_from_record(record)
        if ttl is not None and written is not None:
            break
    return ttl, written


def read_transcript(path, size=None):
    """Scan the transcript tail for the TTL and the time of the last record."""
    if size is None:
        size = os.path.getsize(path)
    ttl, written = scan(read_tail(path, size, TAIL_WINDOW))
    # The window may have landed inside one enormous record, or behind a long run
    # of local records with the last API response above it. Either leaves an
    # answer missing, so try once more with a wider window before giving up.
    if (ttl is None or written is None) and size > TAIL_WINDOW:
        wider_ttl, wider_written = scan(read_tail(path, size, TAIL_WINDOW_WIDE))
        ttl = ttl if ttl is not None else wider_ttl
        written = written if written is not None else wider_written
    return ttl, written


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
    # error worth reporting. Note that stat accepts an integer as a file
    # descriptor, so an unchecked number here would stat something unrelated.
    if not path or not isinstance(path, str):
        return None
    # Resolved fresh every tick: the transcript does not exist yet at session
    # start, and compaction replaces it.
    try:
        size = os.path.getsize(path)
    except OSError:
        return None

    ttl, written = read_transcript(path, size)
    if written is None:
        # No datable API response in the tail. A session that has not made its
        # first call yet is the usual reason, along with an empty or truncated
        # transcript, and there is no honest number to show for either.
        return "%scache ?%s" % (DIM, RESET)
    age = time.time() - written

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
    everything it used to show is gone unless this draws it.
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


def main(argv=None):
    """Render one status line row from the payload on stdin."""
    _force_utf8_stdout()
    argv = sys.argv[1:] if argv is None else argv
    ascii_only = "--ascii" in argv

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

    output = prefix_context(context, segment)
    if output:
        try:
            sys.stdout.write(output + "\n")
        except Exception:
            pass
    return 0
