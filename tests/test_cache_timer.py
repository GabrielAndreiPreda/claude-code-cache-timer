#!/usr/bin/env python3
"""Tests for the cache timer. Run with: python3 -m unittest discover -s tests

Covers the cases a real transcript is unlikely to supply on demand: the 5-minute
cache, records too large for the tail window, a session idle for days, and the
Windows command forms.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# Import from the checkout rather than requiring an install, so the tests run
# against the code in front of you.
sys.path.insert(0, os.path.join(ROOT, "src"))

from cache_timer import cli, install
from cache_timer import statusline as timer


def usage_record(ttl_1h=0, ttl_5m=0, filler=0, age=None):
    """An assistant turn: the only record type an API call produces.

    `age` pins the record's own timestamp instead of taking the transcript's.
    """
    record = {
        "type": "assistant",
        "message": {
            "usage": {
                "cache_creation": {
                    "ephemeral_1h_input_tokens": ttl_1h,
                    "ephemeral_5m_input_tokens": ttl_5m,
                }
            }
        },
    }
    if age is not None:
        record["timestamp"] = iso(time.time() - age)
    if filler:
        record["padding"] = "x" * filler
    return json.dumps(record)


def plain_record(filler=0, record_type="user", age=None):
    """A record Claude Code writes without an API call behind it.

    `age` pins the record's own timestamp instead of taking the transcript's, so
    a test can put a local record newer than the last API response.
    """
    record = {"type": record_type, "message": {"role": "user", "content": "hi"}}
    if age is not None:
        record["timestamp"] = iso(time.time() - age)
    if filler:
        record["padding"] = "x" * filler
    return json.dumps(record)


def slash_command_record(age=None):
    """What running a slash command such as `/exit` leaves in the transcript."""
    record = {
        "type": "user",
        "message": {
            "role": "user",
            "content": "<command-name>/exit</command-name>",
        },
    }
    if age is not None:
        record["timestamp"] = iso(time.time() - age)
    return json.dumps(record)


def strip_ansi(text):
    return re.sub(r"\033\[[0-9;]*m", "", text)


def iso(when):
    """A transcript timestamp for an epoch time, to the millisecond."""
    return "%s.%03dZ" % (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when)),
        (when % 1) * 1000,
    )


class TranscriptCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cache-timer-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def transcript(self, lines, age=0.0):
        """Write a transcript whose records were written `age` seconds ago.

        The file's mtime is left fresh on purpose. Claude Code touches
        transcripts long after the session that wrote them stopped making calls,
        so an aged transcript and an aged file are two different things and the
        countdown must come from the records.
        """
        stamp = iso(time.time() - age)
        path = os.path.join(self.dir, "t.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for line in lines:
                try:
                    record = json.loads(line)
                    # A record that pinned its own timestamp keeps it, so a test
                    # can age records against each other.
                    record.setdefault("timestamp", stamp)
                    line = json.dumps(record)
                except ValueError:
                    pass  # A deliberately unparseable line. Write it as it is.
                handle.write(line + "\n")
        return path

    def ttl(self, path):
        return timer.read_transcript(path)[0]


class TestTtlDetection(TranscriptCase):
    def test_detects_one_hour(self):
        path = self.transcript([usage_record(ttl_1h=4842)])
        self.assertEqual(self.ttl(path), 3600)

    def test_detects_five_minutes(self):
        path = self.transcript([usage_record(ttl_5m=1900)])
        self.assertEqual(self.ttl(path), 300)

    def test_skips_pure_cache_read_turns(self):
        """A turn that only reads the cache writes {0, 0}; keep looking past it."""
        path = self.transcript(
            [usage_record(ttl_5m=1900)] + [usage_record() for _ in range(5)]
        )
        self.assertEqual(self.ttl(path), 300)

    def test_uses_most_recent_write_when_ttl_changes(self):
        """Sessions can drop from 1h to 5m; the newest write wins."""
        path = self.transcript([usage_record(ttl_1h=100), usage_record(ttl_5m=100)])
        self.assertEqual(self.ttl(path), 300)

    def test_no_cache_write_at_all(self):
        path = self.transcript([plain_record(), plain_record()])
        self.assertIsNone(self.ttl(path))

    def test_widens_window_past_a_giant_record(self):
        """One oversized record must not hide the cache write behind it."""
        path = self.transcript(
            [usage_record(ttl_1h=4842), plain_record(filler=400 * 1024)]
        )
        self.assertGreater(os.path.getsize(path), timer.TAIL_WINDOW)
        self.assertEqual(self.ttl(path), 3600)

    def test_survives_unparseable_lines(self):
        path = self.transcript(["{ this is not json", usage_record(ttl_1h=1)])
        self.assertEqual(self.ttl(path), 3600)

    def test_empty_file(self):
        path = self.transcript([])
        self.assertIsNone(self.ttl(path))


class TestClock(TranscriptCase):
    """Where "now minus when" comes from."""

    def test_the_clock_is_the_last_record_not_the_file(self):
        """A touched file must not read as a live cache.

        Claude Code touches old transcripts, so a long-idle session can have a
        fresh mtime. Keyed to that, the countdown restarts from full and reports
        cache that expired days ago.
        """
        path = self.transcript([usage_record(ttl_1h=1)], age=4 * 86400)
        self.assertLess(time.time() - os.path.getmtime(path), 60)
        self.assertIn("cold", strip_ansi(timer.render(path)))

    def test_a_slash_command_does_not_reset_the_clock(self):
        """`/exit` writes a timestamped user record and makes no API call.

        Anchored on the newest timestamp of any kind, the countdown restarted on
        a cache nothing had touched: a 5m cache that went cold 100 seconds ago
        reported the better part of five minutes still on it.
        """
        path = self.transcript(
            [
                usage_record(ttl_5m=1900, age=400),
                slash_command_record(age=0),
            ]
        )
        self.assertIn("cold", strip_ansi(timer.render(path)))

    def test_local_records_do_not_reset_the_clock(self):
        """Every timestamped record type Claude Code writes without a request."""
        for record_type in ("user", "attachment", "file-history-delta", "system"):
            path = self.transcript(
                [
                    usage_record(ttl_5m=1900, age=400),
                    plain_record(record_type=record_type, age=0),
                ]
            )
            self.assertIn("cold", strip_ansi(timer.render(path)), record_type)

    def test_the_clock_is_the_newest_api_response(self):
        """Not the oldest one either -- a later call resets the TTL."""
        path = self.transcript(
            [
                usage_record(ttl_1h=1900, age=3000),
                usage_record(age=60),
                slash_command_record(age=0),
            ]
        )
        # Anchored on the newer response, an hour's cache has ~59 minutes left.
        self.assertIn("59:", strip_ansi(timer.render(path)))

    def test_a_transcript_of_only_local_records_is_undatable(self):
        """No API call yet means no honest number, not a full countdown."""
        path = self.transcript([plain_record(), slash_command_record()])
        self.assertIn("cache ?", strip_ansi(timer.render(path)))

    def test_widens_window_to_reach_the_last_api_response(self):
        """Local records can bury the last response below the narrow window."""
        path = self.transcript(
            [
                usage_record(ttl_1h=4842, age=60),
                plain_record(filler=400 * 1024, age=0),
            ]
        )
        self.assertGreater(os.path.getsize(path), timer.TAIL_WINDOW)
        ttl, written = timer.read_transcript(path)
        self.assertEqual(ttl, 3600)
        self.assertIsNotNone(written)
        self.assertAlmostEqual(time.time() - written, 60, delta=5)

    def test_reads_an_iso_timestamp(self):
        self.assertEqual(timer.epoch("1970-01-01T00:01:00.000Z"), 60)
        self.assertEqual(timer.epoch("1970-01-01T00:01:00Z"), 60)

    def test_the_millisecond_fraction_is_kept(self):
        """Flooring it would leave the countdown up to a second pessimistic."""
        self.assertAlmostEqual(timer.epoch("1970-01-01T00:01:00.750Z"), 60.75)
        # An offset in place of a fraction still yields the whole second.
        self.assertEqual(timer.epoch("1970-01-01T00:01:00+00:00"), 60)

    def test_an_undatable_timestamp_is_not_a_crash(self):
        for value in (None, "", "yesterday", 12345, "2026-13-45T99:99:99Z"):
            self.assertIsNone(timer.epoch(value), value)

    def test_records_without_timestamps_show_nothing_countable(self):
        path = os.path.join(self.dir, "raw.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(usage_record(ttl_1h=1) + "\n")
        self.assertIn("cache ?", strip_ansi(timer.render(path)))


class TestRender(TranscriptCase):
    def test_green_when_over_half_remains(self):
        path = self.transcript([usage_record(ttl_1h=1)], age=60)
        out = timer.render(path)
        self.assertIn(timer.GREEN, out)
        self.assertIn("59:00", strip_ansi(out))
        self.assertIn("1h", strip_ansi(out))

    def test_yellow_between_a_fifth_and_a_half(self):
        path = self.transcript([usage_record(ttl_5m=1)], age=200)
        out = timer.render(path)
        self.assertIn(timer.YELLOW, out)
        self.assertIn("1:40", strip_ansi(out))

    def test_red_under_a_fifth(self):
        path = self.transcript([usage_record(ttl_5m=1)], age=260)
        out = timer.render(path)
        self.assertIn(timer.RED, out)
        self.assertIn("5m", strip_ansi(out))

    def test_cold_when_expired(self):
        path = self.transcript([usage_record(ttl_5m=1)], age=400)
        self.assertIn("cold", strip_ansi(timer.render(path)))

    def test_unknown_ttl_recently_touched(self):
        """A turn that only read the cache dates the call but not the bucket."""
        path = self.transcript([usage_record()], age=10)
        self.assertIn("cache ?", strip_ansi(timer.render(path)))

    def test_unknown_ttl_but_older_than_any_cache(self):
        """Past the longest possible TTL it is cold whichever bucket was used."""
        path = self.transcript([usage_record()], age=7200)
        self.assertIn("cold", strip_ansi(timer.render(path)))

    def test_hours_are_shown_when_present(self):
        self.assertEqual(timer.format_clock(3725), "1:02:05")
        self.assertEqual(timer.format_clock(65), "1:05")
        self.assertEqual(timer.format_clock(5), "0:05")

    def test_ascii_mode_emits_no_emoji(self):
        path = self.transcript([usage_record(ttl_1h=1)], age=60)
        out = strip_ansi(timer.render(path, ascii_only=True))
        self.assertTrue(all(ord(char) < 128 for char in out), out)

    def test_missing_transcript(self):
        self.assertIsNone(timer.render(os.path.join(self.dir, "nope.jsonl")))
        self.assertIsNone(timer.render(None))
        self.assertIsNone(timer.render(""))

    def test_non_string_transcript_path(self):
        """os.stat() takes an fd, so a bare number must not be stat'ed as one."""
        for value in (2, 12345, ["a"], {"b": 1}):
            self.assertIsNone(timer.render(value), value)


class TestContextSegment(unittest.TestCase):
    """Model, directory and branch, which a custom status line displaces."""

    def context(self, payload, **kwargs):
        return strip_ansi(timer.render_context(payload, **kwargs) or "")

    def test_model_and_directory(self):
        out = self.context({"cwd": "/tmp", "model": {"display_name": "Opus"}})
        self.assertEqual(out, "Opus · /tmp")

    def test_workspace_current_dir_wins_over_cwd(self):
        out = self.context({"cwd": "/tmp", "workspace": {"current_dir": "/srv"}})
        self.assertEqual(out, "/srv")

    def test_home_is_abbreviated(self):
        home = os.path.expanduser("~")
        self.assertEqual(self.context({"cwd": home}), "~")
        self.assertEqual(
            self.context({"cwd": os.path.join(home, "proj")}),
            os.path.join("~", "proj"),
        )

    def test_ascii_mode_uses_a_plain_separator(self):
        out = self.context(
            {"cwd": "/tmp", "model": {"display_name": "Opus"}}, ascii_only=True
        )
        self.assertEqual(out, "Opus - /tmp")
        self.assertTrue(all(ord(char) < 128 for char in out), out)

    def test_nothing_to_draw(self):
        self.assertIsNone(timer.render_context({}))
        self.assertIsNone(timer.render_context(None))
        self.assertIsNone(timer.render_context("not a dict"))

    def test_malformed_fields_are_ignored(self):
        self.assertIsNone(timer.render_context({"model": "Opus", "cwd": 12}))
        self.assertIsNone(timer.render_context({"model": {"display_name": 5}}))

    def test_prefix_joins_context_and_segment(self):
        self.assertEqual(timer.prefix_context("CTX", "SEG"), "CTX  SEG")
        self.assertEqual(timer.prefix_context("CTX", None), "CTX")
        self.assertEqual(timer.prefix_context(None, "SEG"), "SEG")
        self.assertIsNone(timer.prefix_context(None, None))


class TestGitBranch(unittest.TestCase):
    """Read from .git/HEAD; spawning git once a second is not affordable."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cache-timer-git-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def make_repo(self, head, at=None):
        root = at or self.dir
        git = os.path.join(root, ".git")
        os.makedirs(git, exist_ok=True)
        with open(os.path.join(git, "HEAD"), "w", encoding="utf-8") as handle:
            handle.write(head)
        return root

    def test_reads_the_branch(self):
        self.make_repo("ref: refs/heads/main\n")
        self.assertEqual(timer.git_branch(self.dir), "main")

    def test_slashes_in_branch_names_survive(self):
        self.make_repo("ref: refs/heads/feature/nested/thing\n")
        self.assertEqual(timer.git_branch(self.dir), "feature/nested/thing")

    def test_detached_head_shows_a_short_sha(self):
        self.make_repo("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0\n")
        self.assertEqual(timer.git_branch(self.dir), "a1b2c3d")

    def test_found_from_a_subdirectory(self):
        self.make_repo("ref: refs/heads/main\n")
        deep = os.path.join(self.dir, "a", "b", "c")
        os.makedirs(deep)
        self.assertEqual(timer.git_branch(deep), "main")

    def test_gitdir_pointer_file(self):
        """Linked worktrees and submodules use a .git file, not a directory."""
        real = os.path.join(self.dir, "real")
        os.makedirs(real)
        with open(os.path.join(real, "HEAD"), "w", encoding="utf-8") as handle:
            handle.write("ref: refs/heads/wt\n")
        work = os.path.join(self.dir, "work")
        os.makedirs(work)
        with open(os.path.join(work, ".git"), "w", encoding="utf-8") as handle:
            handle.write("gitdir: %s\n" % real)
        self.assertEqual(timer.git_branch(work), "wt")

    def test_outside_a_repository(self):
        self.assertIsNone(timer.git_branch(self.dir))

    def test_unreadable_repository_is_not_an_error(self):
        os.makedirs(os.path.join(self.dir, ".git"))  # no HEAD inside
        self.assertIsNone(timer.git_branch(self.dir))
        self.assertIsNone(timer.git_branch("/does/not/exist"))

    def test_real_repository(self):
        """This checkout, read without shelling out to git.

        Skipped when there is nothing to compare against: a tarball download
        rather than a clone, or a machine with no git installed.
        """
        if timer.find_git_dir(ROOT) is None:
            self.skipTest("not a git checkout")
        try:
            probe = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError:
            self.skipTest("git is not installed")
        if probe.returncode != 0:
            self.skipTest("git could not resolve HEAD")
        self.assertEqual(timer.git_branch(ROOT), probe.stdout.strip())


class TestSubprocess(TranscriptCase):
    """End-to-end through the real entry point, the way Claude Code invokes it."""

    def run_script(self, payload, extra_args=(), env=None):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.join(ROOT, "src")
        if env:
            environment.update(env)
        result = subprocess.run(
            [sys.executable, "-m", "cache_timer"] + list(extra_args),
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            env=environment,
        )
        return result

    def test_renders_a_countdown(self):
        path = self.transcript([usage_record(ttl_1h=1)], age=30)
        result = self.run_script(json.dumps({"transcript_path": path}))
        self.assertEqual(result.returncode, 0)
        self.assertIn("59:30", strip_ansi(result.stdout))

    def test_never_writes_to_stderr_or_fails(self):
        """A crash would blank the status line row once a second."""
        cases = [
            "",
            "not json",
            "{}",
            "[]",
            "null",
            json.dumps({"transcript_path": "/does/not/exist.jsonl"}),
            json.dumps({"transcript_path": 12345}),
            json.dumps({"transcript_path": self.dir}),  # a directory
        ]
        for payload in cases:
            result = self.run_script(payload)
            self.assertEqual(result.returncode, 0, payload)
            self.assertEqual(result.stderr, "", payload)

    def test_shows_the_context_from_the_payload(self):
        """A custom status line replaces the built-in one, context included."""
        path = self.transcript([usage_record(ttl_1h=1)], age=30)
        result = self.run_script(
            json.dumps(
                {
                    "transcript_path": path,
                    "model": {"display_name": "Opus"},
                    "workspace": {"current_dir": HERE},
                }
            )
        )
        self.assertEqual(result.returncode, 0)
        out = strip_ansi(result.stdout)
        self.assertIn("Opus · ", out)
        self.assertIn(os.path.basename(HERE), out)
        self.assertIn("59:30", out)

    def test_survives_a_cp1252_stdout(self):
        """Windows Python defaults stdout to the ANSI code page."""
        path = self.transcript([usage_record(ttl_1h=1)], age=30)
        result = self.run_script(
            json.dumps({"transcript_path": path}), env={"PYTHONIOENCODING": "cp1252"}
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertIn("59:30", strip_ansi(result.stdout))


class TestCli(unittest.TestCase):
    """Dispatch. The no-subcommand form is what settings.json invokes."""

    def run_cli(self, argv, stdin=None):
        """Run the entry point with its streams captured."""
        saved = sys.stdout, sys.stderr, sys.stdin
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        if stdin is not None:
            sys.stdin = stdin
        try:
            code = cli.main(argv)
            return code, sys.stdout.getvalue() + sys.stderr.getvalue()
        finally:
            sys.stdout, sys.stderr, sys.stdin = saved

    def test_version(self):
        code, out = self.run_cli(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("claude-cache-timer", out)

    def test_help(self):
        code, out = self.run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)

    def test_unknown_argument_is_rejected(self):
        code, out = self.run_cli(["--nope"])
        self.assertEqual(code, 2)
        self.assertIn("--nope", out)

    def test_help_is_shown_when_run_interactively(self):
        """A bare invocation at a prompt must not hang on a read of stdin."""
        terminal = io.StringIO()
        terminal.isatty = lambda: True
        code, out = self.run_cli([], stdin=terminal)
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)

    def test_a_piped_payload_is_rendered(self):
        code, out = self.run_cli([], stdin=io.StringIO("{}"))
        self.assertEqual(code, 0)
        self.assertEqual(out, "")


class FakeShutil:
    """Stands in for the shutil install.py uses, so PATH can be controlled."""

    def __init__(self, which):
        self._which = which

    def which(self, name):
        return self._which.get(name)

    def __getattr__(self, name):
        return getattr(shutil, name)


class TestCandidate(unittest.TestCase):
    """What ends up in settings.json, and what happens when it cannot."""

    def patch(self, name, value):
        original = getattr(install, name)
        setattr(install, name, value)
        self.addCleanup(setattr, install, name, original)

    def test_bare_name_when_the_console_script_is_on_path(self):
        """No path, no quoting, so no shell can mangle it."""
        self.patch("shutil", FakeShutil(which={install.CONSOLE_SCRIPT: "/usr/bin/cct"}))
        chosen = install.candidate()
        self.assertEqual(chosen.command(), install.CONSOLE_SCRIPT)
        # settings.json gets the bare name; verification runs the resolved path.
        self.assertEqual(chosen.argv[0], "/usr/bin/cct")

    def test_ascii_flag_is_appended(self):
        self.patch("shutil", FakeShutil(which={install.CONSOLE_SCRIPT: "/usr/bin/cct"}))
        chosen = install.candidate(ascii_only=True)
        self.assertEqual(chosen.command(), install.CONSOLE_SCRIPT + " --ascii")

    def test_nothing_is_installable_when_the_script_is_not_on_path(self):
        """Rather than write an absolute path that a shell would have to quote."""
        self.patch("shutil", FakeShutil(which={}))
        self.assertIsNone(install.candidate())

    def test_a_missing_script_is_a_hard_stop_with_the_fix(self):
        self.patch("shutil", FakeShutil(which={}))
        with self.assertRaises(SystemExit) as caught:
            install.choose()
        message = str(caught.exception)
        self.assertIn("not on PATH", message)
        self.assertIn("update-shell", message)
        self.assertIn("ensurepath", message)
        self.assertIn("Nothing was written", message)


class TestInstallerVerification(unittest.TestCase):
    def module_candidate(self):
        # Not a form the installer ever writes, but verify() only needs an argv,
        # and this is the handiest one that renders a real countdown.
        return install.Candidate(
            [sys.executable, "-m", "cache_timer"],
            [sys.executable, "-m", "cache_timer"],
        )

    def setUp(self):
        # `python -m cache_timer` only resolves from an install or a path entry.
        self.environ = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = os.path.join(ROOT, "src")
        self.addCleanup(self.restore)

    def restore(self):
        if self.environ is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = self.environ

    def test_verify_accepts_the_real_command(self):
        ok, detail = install.verify(self.module_candidate())
        self.assertTrue(ok, detail)
        # The fixture is written with a fresh mtime, so the full hour is left.
        self.assertIn("1:00:00", strip_ansi(detail))
        self.assertIn("1h", strip_ansi(detail))
        # The context prefix must be covered at install time, not just here.
        self.assertIn("Opus", strip_ansi(detail))

    def test_verify_rejects_a_silent_command(self):
        ok, _ = install.verify(install.Candidate(["true"], ["true"]))
        self.assertFalse(ok)

    def test_verify_rejects_a_broken_command(self):
        name = "this-command-does-not-exist-xyz"
        ok, _ = install.verify(install.Candidate([name], [name]))
        self.assertFalse(ok)


class SettingsCase(unittest.TestCase):
    """A throwaway settings.json, so no test can touch the real one."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cache-timer-settings-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.settings = os.path.join(self.dir, "settings.json")
        self.patched = {
            "SETTINGS": install.SETTINGS,
            "BACKUP_DIR": install.BACKUP_DIR,
        }
        install.SETTINGS = self.settings
        install.BACKUP_DIR = os.path.join(self.dir, "backups")
        self.addCleanup(self.restore)

    def restore(self):
        for key, value in self.patched.items():
            setattr(install, key, value)

    def write(self, data):
        with open(self.settings, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)

    def read(self):
        with open(self.settings, "r", encoding="utf-8") as handle:
            return json.load(handle)


class TestSettingsRoundTrip(SettingsCase):
    """Reading and rewriting settings.json without losing anything."""

    def test_preserves_unrelated_settings(self):
        self.write({"model": "opus", "hooks": {"Stop": [{"matcher": ""}]}})
        settings = install.load_settings()
        settings["statusLine"] = {"type": "command", "command": "x"}
        install.write_settings(settings)
        after = self.read()
        self.assertEqual(after["model"], "opus")
        self.assertEqual(after["hooks"], {"Stop": [{"matcher": ""}]})

    def test_backup_is_written(self):
        self.write({"model": "opus"})
        backup = install.backup_settings()
        self.assertTrue(os.path.exists(backup))
        with open(backup, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["model"], "opus")

    def test_backups_in_the_same_second_do_not_collide(self):
        """Install then uninstall must not destroy the pre-install copy."""
        self.write({"generation": 1})
        first = install.backup_settings()
        self.write({"generation": 2})
        second = install.backup_settings()
        self.assertNotEqual(first, second)
        with open(first, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["generation"], 1)
        with open(second, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["generation"], 2)

    def test_missing_settings_file_is_fine(self):
        self.assertEqual(install.load_settings(), {})
        self.assertIsNone(install.backup_settings())

    def test_empty_settings_file_is_fine(self):
        with open(self.settings, "w", encoding="utf-8") as handle:
            handle.write("")
        self.assertEqual(install.load_settings(), {})

    def test_refuses_a_settings_file_with_comments(self):
        """Claude Code allows JSONC here; rewriting it would strip the comments."""
        with open(self.settings, "w", encoding="utf-8") as handle:
            handle.write('{\n  // my model\n  "model": "opus"\n}\n')
        with self.assertRaises(install.UnreadableSettings):
            install.load_settings()

    def test_refuses_a_settings_file_that_is_not_an_object(self):
        with open(self.settings, "w", encoding="utf-8") as handle:
            handle.write("[1, 2, 3]")
        with self.assertRaises(install.UnreadableSettings):
            install.load_settings()

    def test_manual_instructions_show_what_to_paste(self):
        status_line = {"type": "command", "command": "x", "refreshInterval": 1}
        text = install.manual_instructions("bad comma", status_line)
        self.assertIn("Nothing was changed", text)
        self.assertIn('"statusLine"', text)
        self.assertIn('"refreshInterval": 1', text)

    def test_unparseable_settings_leaves_the_file_alone(self):
        original = '{\n  // keep me\n  "model": "opus"\n}\n'
        with open(self.settings, "w", encoding="utf-8") as handle:
            handle.write(original)
        try:
            install.load_settings()
        except install.UnreadableSettings:
            pass
        with open(self.settings, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)


class TestInstallCommand(SettingsCase):
    """install() and uninstall() end to end, with verification stubbed out."""

    def setUp(self):
        super().setUp()
        fake = install.Candidate([install.CONSOLE_SCRIPT], ["/usr/bin/cct"])
        self.patched_choose = install.choose
        install.choose = lambda ascii_only=False: (fake, "rendered")
        self.addCleanup(setattr, install, "choose", self.patched_choose)

    def run_command(self, function, argv):
        """Run a command with stdout captured; returns (exit code, output)."""
        stdout, sys.stdout = sys.stdout, io.StringIO()
        try:
            code = function(argv)
            return code, sys.stdout.getvalue()
        finally:
            sys.stdout = stdout

    def status_line(self):
        return self.read().get("statusLine")

    def test_writes_the_bare_command(self):
        code, _ = self.run_command(install.install, [])
        self.assertEqual(code, 0)
        self.assertEqual(
            self.status_line(),
            {
                "type": "command",
                "command": install.CONSOLE_SCRIPT,
                "refreshInterval": 1,
            },
        )

    def test_dry_run_writes_nothing(self):
        self.write({"model": "opus"})
        self.run_command(install.install, ["--dry-run"])
        self.assertIsNone(self.status_line())

    def test_a_foreign_status_line_is_refused_without_force(self):
        """Silently discarding somebody's status line is worse than a flag."""
        self.write({"statusLine": {"type": "command", "command": "my-own-thing"}})
        with self.assertRaises(SystemExit):
            self.run_command(install.install, [])
        self.assertEqual(self.status_line()["command"], "my-own-thing")

    def test_force_replaces_a_foreign_status_line(self):
        self.write({"statusLine": {"type": "command", "command": "my-own-thing"}})
        self.run_command(install.install, ["--force"])
        self.assertEqual(self.status_line()["command"], install.CONSOLE_SCRIPT)

    def test_reinstalling_over_our_own_needs_no_force(self):
        self.write({"statusLine": {"type": "command", "command": "claude-cache-timer"}})
        code, _ = self.run_command(install.install, ["--interval", "3"])
        self.assertEqual(code, 0)
        self.assertEqual(self.status_line()["refreshInterval"], 3)

    def test_the_full_path_form_is_recognised_as_ours(self):
        """A PATH-less install writes a path, and reinstalling must know it."""
        old = '"/home/ada/.local/bin/claude-cache-timer" --ascii'
        self.assertTrue(install.is_ours(old))
        self.write({"statusLine": {"type": "command", "command": old}})
        code, _ = self.run_command(install.install, [])
        self.assertEqual(code, 0)
        self.assertEqual(self.status_line()["command"], install.CONSOLE_SCRIPT)

    def test_padding_is_carried_over(self):
        self.write({"statusLine": {"command": "claude-cache-timer", "padding": 0}})
        self.run_command(install.install, [])
        self.assertEqual(self.status_line()["padding"], 0)

    def test_uninstall_removes_the_entry(self):
        self.write({"model": "opus", "statusLine": {"command": "claude-cache-timer"}})
        code, _ = self.run_command(install.uninstall, [])
        self.assertEqual(code, 0)
        self.assertIsNone(self.status_line())
        self.assertEqual(self.read()["model"], "opus")

    def test_uninstall_leaves_a_foreign_status_line_alone(self):
        self.write({"statusLine": {"command": "my-own-thing"}})
        self.run_command(install.uninstall, [])
        self.assertEqual(self.status_line()["command"], "my-own-thing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
