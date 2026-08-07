#!/usr/bin/env python3
"""Remove the cache timer status line from ~/.claude/settings.json.

Restores whatever status line was configured before install, if any. Backs up
settings.json first. Leaves every other setting, including hooks, untouched.
"""

import json
import os
import sys

# Imported as a module, not as names, so the paths stay a single source of truth
# and the tests can redirect them at a throwaway home.
import install


def main():
    if not os.path.exists(install.SETTINGS):
        print("No %s; nothing to do." % install.SETTINGS)
        return 0

    try:
        settings = install.load_settings()
    except install.UnreadableSettings as error:
        raise SystemExit(install.manual_instructions(error))
    existing = settings.get("statusLine")
    ours = isinstance(existing, dict) and install.MARKER in str(
        existing.get("command", "")
    )

    wrapped = None
    if os.path.exists(install.WRAPPED):
        try:
            with open(install.WRAPPED, "r", encoding="utf-8") as handle:
                candidate = json.load(handle)
            if isinstance(candidate, dict) and candidate.get("command"):
                wrapped = candidate
        except Exception as error:
            print("warning: could not read %s: %s" % (install.WRAPPED, error))

    if not ours and wrapped is None:
        print("The cache timer status line is not installed; nothing to do.")
        return 0

    if not ours:
        print(
            "warning: the current statusLine is not the cache timer, so it was\n"
            "         left alone. Removing the saved wrapper file only."
        )
    else:
        backup = install.backup_settings()
        if backup:
            print("backed up settings.json -> %s" % backup)
        if wrapped is not None:
            settings["statusLine"] = wrapped
            print("restored your previous status line: %s" % wrapped.get("command"))
        else:
            settings.pop("statusLine", None)
            print("removed the statusLine entry")
        install.write_settings(settings)

    if os.path.exists(install.WRAPPED):
        os.unlink(install.WRAPPED)
        # Install created this directory to hold the file; take it back out,
        # unless something else has since moved in.
        try:
            os.rmdir(os.path.dirname(install.WRAPPED))
        except OSError:
            pass

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
