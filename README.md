# claude-code-cache-timer

A Claude Code status line segment that counts down the time left on your session's
prompt cache.

```
Opus · ~/my-project · main    ⏳ 52:18 · 1h
```

When the cache lapses, the next turn re-writes the entire prompt prefix at 1.25x
(5-minute cache) or 2x (1-hour cache) the base input rate. Nothing in the UI tells you
how much time is left, so you can't tell whether to keep typing now or go make coffee.

| Display | Meaning |
|---|---|
| `⏳ 52:18 · 1h` green | over half the TTL remains |
| `⏳ 21:04 · 1h` yellow | between a fifth and a half |
| `⏳ 3:41 · 5m` red | under a fifth, spend it or lose it |
| `❄️ cold` | expired; the next turn pays to rewrite the prefix |
| `cache ?` | no cache write on record yet, TTL unknown |

Configuring any status line replaces Claude Code's built-in one, so the model, directory
and branch it used to show would otherwise vanish. They are redrawn here, dimmed, ahead of
the countdown. If you are wrapping an existing status line, that command already draws the
row and the prefix is left off.

The branch is read straight out of `.git/HEAD` rather than by running `git`, which costs
7 µs instead of 780 µs. Linked worktrees and submodules, whose `.git` is a pointer file
rather than a directory, are followed; a detached HEAD shows a short SHA.

## Install

Requires Python 3.8 or newer. Linux, macOS, and Windows.

```sh
git clone https://github.com/GabrielAndreiPreda/claude-code-cache-timer
cd claude-code-cache-timer
python3 install.py
```

Then open a new Claude Code session. To preview without writing anything, use
`--dry-run`.

The installer adds a `statusLine` entry to `~/.claude/settings.json` and copies the old
file into `~/.claude/backups/` first. It changes nothing else and leaves your hooks and
permissions alone.

Claude Code allows comments and trailing commas in `settings.json`, which Python's JSON
parser rejects. If your file has either, the installer stops without touching it and
prints the block to paste in yourself, rather than rewriting the file and stripping your
comments.

If you already have a status line, the installer keeps it and wraps it. Your command
still runs and still gets the same JSON payload, and the cache segment is appended to its
last line. The original goes to `~/.claude/cache-timer/wrapped.json`, and uninstall puts
it back.

Options:

| Flag | Effect |
|---|---|
| `--ascii` | use `~` and `*` instead of emoji, for terminals that render them badly |
| `--interval N` | seconds between refreshes (default 1) |
| `--dry-run` | verify and print the change without writing |

```sh
python3 uninstall.py
```

## How it works

Only one quantity matters: when this session last made an API call. Every call that hits
the cache resets the TTL.

```
remaining = ttl - (now - last_api_call)
```

The clock is the transcript's mtime. `~/.claude/projects/<slug>/<session_id>.jsonl` gets
appended on every message, so its mtime advances on every API call the session makes,
including the many that no hook reports.

Subagents come out right for free. They write to a `subagents/` subdirectory, and their
calls do not refresh the parent's cache. So while a subagent runs, the parent transcript's
mtime stalls and the countdown keeps draining, which is what you want to see. "The agent
is busy, so the cache must be fine" is wrong: a long-running subagent, a long build, or a
permission prompt you walked away from all drain the cache while the session looks like
it's working.

The TTL is read, never assumed. Sessions run on either the 5-minute or the 1-hour cache,
and can move between them mid-session. The transcript says which:

```json
"cache_creation": { "ephemeral_1h_input_tokens": 19627, "ephemeral_5m_input_tokens": 0 }
```

A hardcoded 5-minute countdown is wrong by a factor of 12 on a 1-hour session. The script
walks the transcript tail backwards for the newest non-zero bucket, re-reading every tick.
Turns that only read the cache record `{0, 0}`, so those get skipped rather than mistaken
for an answer.

There are no hooks, and no state file unless you are wrapping an existing status line.
Everything needed arrives in the status line's own stdin payload (`transcript_path`,
`model.display_name`, `workspace.current_dir`), once a second via `refreshInterval`.
[CACHE-MECHANISM.md](CACHE-MECHANISM.md) documents all of this in full.

## Limitations

Claude Code hides the status line during permission dialogs, autocomplete, and the help
menu. That is precisely when you are most likely to be idling the cache away, and the
number is invisible for it. Covering that case would need a separate always-on ticker
outside Claude Code.

The number reads optimistic by up to one response duration. The TTL restarts when a
request is sent, but the transcript is written when the response completes. On a slow turn
the display runs high by the length of that turn, which is negligible against an hour and
worth knowing against five minutes.

Each session spawns one process per second. That costs about 19 ms on Linux, of which
about 12 ms is Python interpreter startup that no amount of tuning inside the script can
recover; several times more on Windows, where process creation and Python startup are both
slower. Raise `refreshInterval` to `2` in `settings.json` if you would rather.

Wrapped commands run under `/bin/sh` on Unix and `cmd.exe` on Windows, which may not be
the shell yours was written for. Wrapping a Git Bash status line on Windows can need the
command adjusted by hand.

The status line also requires that you have accepted the workspace trust dialog, the same
gate that applies to hooks.

## Tests

```sh
python3 test_cache_timer.py
```

63 tests covering TTL detection (both buckets, mid-session changes, records too large for
the tail window), the colour bands, graceful degradation on every malformed input, branch
reading (worktrees, submodules, detached HEAD), wrap mode, and the three Windows and Unix
command-quoting forms.

Windows quoting is unit-tested. The installer executes the command it generates before writing
anything, so a quoting mistake fails loudly at install time instead of leaving you a
silently blank status line.

## License

GNU GPL v3.0
