"""Argument dispatch for the ``claude-cache-timer`` command.

Rendering the status line is the no-subcommand form, because that is the string
that ends up in settings.json and Claude Code runs it once a second. Install and
uninstall are subcommands, and their module is imported only when one of them is
asked for, so the once-a-second path does not pay to load it.
"""

import sys

from . import __version__

USAGE = """usage: claude-cache-timer [--ascii]
       claude-cache-timer install [--ascii] [--interval N] [--dry-run] [--force]
       claude-cache-timer uninstall

With no subcommand, reads the Claude Code status line payload on stdin and
prints the cache countdown. That is how Claude Code invokes it; run
`claude-cache-timer install` to configure it as your status line.

  --ascii      use ASCII instead of emoji, for terminals that render them badly
  --interval   seconds between refreshes (install only, default 1)
  --dry-run    verify and print the change without writing settings.json
  --force      replace an existing status line that is not this one
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else None

    if command in ("install", "uninstall"):
        from . import install

        handler = install.install if command == "install" else install.uninstall
        return handler(argv[1:])

    if command in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0

    if command in ("-V", "--version"):
        sys.stdout.write("claude-cache-timer %s\n" % __version__)
        return 0

    unknown = [argument for argument in argv if argument != "--ascii"]
    if unknown:
        sys.stderr.write("unknown argument: %s\n\n%s" % (unknown[0], USAGE))
        return 2

    # Someone typing the bare name to see what it does would otherwise get a
    # read on stdin that never returns.
    try:
        interactive = sys.stdin.isatty()
    except Exception:
        interactive = False
    if interactive:
        sys.stdout.write(USAGE)
        return 0

    from . import statusline

    try:
        return statusline.main(argv)
    except Exception:
        # Last line of defence. A traceback on stderr every second would be
        # worse than showing nothing.
        return 0
