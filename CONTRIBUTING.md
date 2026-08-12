# Contributing

Bug reports and pull requests are welcome.

## Two constraints to know first

The status line runs once a second in every open session, so anything added to the render
path is paid for on every tick. This is why the branch is read out of `.git/HEAD` rather
than by running `git`, and why `install.py` is imported inside the subcommand branch of
`cli.py` rather than at the top.

It also may not raise or write to stderr. Claude Code renders whatever the command prints,
so a traceback becomes the status line and a non-zero exit becomes a blank row. Every entry
point swallows exceptions and returns 0. If a value cannot be computed, print less rather
than an error.

## Running it

The package has no dependencies and the tests import from the checkout, so there is nothing
to install.

```sh
git clone https://github.com/GabrielAndreiPreda/claude-code-cache-timer
cd claude-code-cache-timer

python3 -m unittest discover -s tests
```

To run the status line by hand, feed it the payload Claude Code would. Any real transcript
under `~/.claude/projects/` works; use one from a recent session, or the countdown will
read cold.

```sh
echo '{"transcript_path":"'$HOME'/.claude/projects/<slug>/<session>.jsonl",
       "model":{"display_name":"Opus"},
       "workspace":{"current_dir":"'$PWD'"}}' \
  | PYTHONPATH=src python3 -m cache_timer
```

To exercise the installer without changing your own configuration:

```sh
PYTHONPATH=src python3 -m cache_timer install --dry-run
```

`--dry-run` verifies the command and prints what it would write. Without it, the installer
backs `~/.claude/settings.json` up to `~/.claude/backups/` before changing anything.

## Style

No runtime dependencies.

Use `%` formatting rather than f-strings. `requires-python` is `>=3.8` and CI tests that
floor, so newer syntax fails there.

Comments explain why rather than what. If you change a decision one of them documents,
update the comment with it.

Tests go in `tests/test_cache_timer.py`. They cover cases a live session will not produce on
demand: a 5-minute cache, a record too large for the tail window, a session idle for days,
and a transcript touched long after its last API call.

## Pull requests

Run the tests first. CI runs them again on Linux, macOS and Windows across Python 3.8
through 3.13, which catches assumptions about POSIX paths and locale encodings.

Describe what the change does and why. If it changes what the status line displays, include
a before and after of the row.

## Reporting a bug

Open an issue. The template asks for your OS, Python version, Claude Code version, install
method, and the `statusLine` block from `~/.claude/settings.json`.

If the displayed number looks wrong, include the output of `--version` and, if you can, the
last few lines of the transcript it was reading. Redact them first: transcripts contain your
conversation.

## How it works

[CACHE-MECHANISM.md](CACHE-MECHANISM.md) documents the mechanism in full, including why the
clock is the last assistant turn rather than the newest record or the file's mtime, and why
there are no hooks. It is worth reading before changing `statusline.py`.
