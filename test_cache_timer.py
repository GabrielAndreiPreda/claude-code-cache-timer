#!/usr/bin/env python3
"""Tests for the cache timer. Run with: python3 test_cache_timer.py

Covers the cases real transcripts on this machine cannot supply: the 5-minute
cache, records too large for the tail window, and the Windows quoting forms.
"""

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
sys.path.insert(0, HERE)

import cache_timer_statusline as timer
import install

SCRIPT = os.path.join(HERE, "cache_timer_statusline.py")


def usage_record(ttl_1h=0, ttl_5m=0, filler=0):
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
    if filler:
        record["padding"] = "x" * filler
    return json.dumps(record)


def plain_record(filler=0):
    record = {"type": "user", "message": {"role": "user", "content": "hi"}}
    if filler:
        record["padding"] = "x" * filler
    return json.dumps(record)


def strip_ansi(text):
    return re.sub(r"\033\[[0-9;]*m", "", text)


class TranscriptCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cache-timer-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def transcript(self, lines, age=0.0):
        path = os.path.join(self.dir, "t.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")
        if age:
            when = time.time() - age
            os.utime(path, (when, when))
        return path


class TestTtlDetection(TranscriptCase):
    def test_detects_one_hour(self):
        path = self.transcript([usage_record(ttl_1h=4842)])
        self.assertEqual(timer.find_ttl(path), 3600)

    def test_detects_five_minutes(self):
        path = self.transcript([usage_record(ttl_5m=1900)])
        self.assertEqual(timer.find_ttl(path), 300)

    def test_skips_pure_cache_read_turns(self):
        """A turn that only reads the cache writes {0, 0}; keep looking past it."""
        path = self.transcript(
            [usage_record(ttl_5m=1900)] + [usage_record() for _ in range(5)]
        )
        self.assertEqual(timer.find_ttl(path), 300)

    def test_uses_most_recent_write_when_ttl_changes(self):
        """Sessions can drop from 1h to 5m; the newest write wins."""
        path = self.transcript([usage_record(ttl_1h=100), usage_record(ttl_5m=100)])
        self.assertEqual(timer.find_ttl(path), 300)

    def test_no_cache_write_at_all(self):
        path = self.transcript([plain_record(), plain_record()])
        self.assertIsNone(timer.find_ttl(path))

    def test_widens_window_past_a_giant_record(self):
        """One oversized record must not hide the cache write behind it."""
        path = self.transcript(
            [usage_record(ttl_1h=4842), plain_record(filler=400 * 1024)]
        )
        self.assertGreater(os.path.getsize(path), timer.TAIL_WINDOW)
        self.assertEqual(timer.find_ttl(path), 3600)

    def test_survives_unparseable_lines(self):
        path = self.transcript(["{ this is not json", usage_record(ttl_1h=1)])
        self.assertEqual(timer.find_ttl(path), 3600)

    def test_empty_file(self):
        path = self.transcript([])
        self.assertIsNone(timer.find_ttl(path))


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
        path = self.transcript([plain_record()], age=10)
        self.assertIn("cache ?", strip_ansi(timer.render(path)))

    def test_unknown_ttl_but_older_than_any_cache(self):
        """Past the longest possible TTL it is cold whichever bucket was used."""
        path = self.transcript([plain_record()], age=7200)
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
        if timer.find_git_dir(HERE) is None:
            self.skipTest("not a git checkout")
        try:
            probe = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=HERE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                universal_newlines=True,
            )
        except OSError:
            self.skipTest("git is not installed")
        if probe.returncode != 0:
            self.skipTest("git could not resolve HEAD")
        self.assertEqual(timer.git_branch(HERE), probe.stdout.strip())


class TestSubprocess(TranscriptCase):
    """End-to-end through the real script, the way Claude Code invokes it."""

    def run_script(self, payload, extra_args=(), env=None):
        environment = dict(os.environ)
        if env:
            environment.update(env)
        result = subprocess.run(
            [sys.executable, SCRIPT] + list(extra_args),
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
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


class TestWrapMode(TranscriptCase):
    def setUp(self):
        super().setUp()
        self.wrapped_path = os.path.join(self.dir, "wrapped.json")
        self.original = timer.WRAPPED_CONFIG
        timer.WRAPPED_CONFIG = self.wrapped_path
        self.addCleanup(setattr, timer, "WRAPPED_CONFIG", self.original)

    def set_wrapped(self, command):
        with open(self.wrapped_path, "w", encoding="utf-8") as handle:
            json.dump({"type": "command", "command": command}, handle)

    def test_appends_to_the_wrapped_output(self):
        self.set_wrapped("echo hello")
        self.assertEqual(timer.load_wrapped(), "echo hello")
        self.assertEqual(timer.combine("hello", "SEG"), "hello  SEG")

    def test_appends_to_the_last_line_only(self):
        self.assertEqual(timer.combine("one\ntwo", "SEG"), "one\ntwo  SEG")

    def test_segment_alone_when_wrapped_command_fails(self):
        self.assertEqual(timer.combine(None, "SEG"), "SEG")

    def test_wrapped_output_alone_when_segment_fails(self):
        self.assertEqual(timer.combine("hello", None), "hello")

    def test_failing_command_yields_nothing(self):
        self.assertIsNone(timer.run_wrapped("exit 1", "{}"))

    def test_wrapped_output_suppresses_our_context(self):
        """The wrapped command draws the row, so it shows its own context."""
        out = strip_ansi(timer.compose("theirs", "SEG", "CTX"))
        self.assertEqual(out, "theirs  SEG")

    def test_context_is_drawn_when_no_wrapped_output(self):
        """Covers both no wrap configured and a wrap that produced nothing."""
        self.assertEqual(strip_ansi(timer.compose(None, "SEG", "CTX")), "CTX  SEG")

    def test_wrapped_command_receives_the_payload(self):
        # Echoes stdin via this interpreter rather than `cat`, which does not
        # exist under cmd.exe on Windows.
        script = os.path.join(self.dir, "echo_payload.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("import sys; sys.stdout.write(sys.stdin.read())\n")
        payload = json.dumps({"transcript_path": "/x", "session_id": "abc"})
        out = timer.run_wrapped('"%s" "%s"' % (sys.executable, script), payload)
        self.assertIn("abc", out)


class TestCommandBuilder(unittest.TestCase):
    """The three quoting forms. Getting these wrong fails silently."""

    def test_unix_form(self):
        command = install.build_command(
            ["/usr/bin/python3"], "/home/a/s.py", powershell=False
        )
        self.assertEqual(command, '"/usr/bin/python3" "/home/a/s.py"')

    def test_windows_git_bash_form(self):
        command = install.build_command(
            ["C:\\Python\\python.exe"],
            "C:\\Users\\Ada Byron\\s.py",
            powershell=False,
        )
        self.assertEqual(
            command, '"C:/Python/python.exe" "C:/Users/Ada Byron/s.py"'
        )
        self.assertNotIn("\\", command)

    def test_windows_powershell_form_uses_the_call_operator(self):
        command = install.build_command(
            ["C:\\Python\\python.exe"],
            "C:\\Users\\Ada Byron\\s.py",
            powershell=True,
        )
        self.assertTrue(command.startswith("& "), command)
        self.assertNotIn("\\", command)

    def test_py_launcher_needs_no_call_operator(self):
        """A bare command name runs fine in PowerShell unquoted."""
        command = install.build_command(
            ["py", "-3"], "C:/Users/a/s.py", powershell=True
        )
        self.assertFalse(command.startswith("&"), command)
        self.assertEqual(command, 'py -3 "C:/Users/a/s.py"')

    def test_ascii_flag_is_appended(self):
        command = install.build_command(
            ["/usr/bin/python3"], "/home/a/s.py", ascii_only=True
        )
        self.assertTrue(command.endswith(" --ascii"), command)

    def test_paths_never_contain_backslashes(self):
        """Git Bash eats unquoted backslashes and fails with no visible error."""
        for powershell in (True, False):
            command = install.build_command(
                ["C:\\P\\python.exe"], "C:\\U\\s.py", powershell=powershell
            )
            self.assertNotIn("\\", command)


class TestInstallerVerification(unittest.TestCase):
    def test_verify_accepts_the_real_command(self):
        command = install.build_command([sys.executable], SCRIPT)
        ok, detail = install.verify(command)
        self.assertTrue(ok, detail)
        # The fixture is written with a fresh mtime, so the full hour is left.
        self.assertIn("1:00:00", strip_ansi(detail))
        self.assertIn("1h", strip_ansi(detail))
        # The context prefix must be covered at install time, not just here.
        self.assertIn("Opus", strip_ansi(detail))

    def test_verify_rejects_a_silent_command(self):
        ok, _ = install.verify("true")
        self.assertFalse(ok)

    def test_verify_rejects_a_broken_command(self):
        ok, _ = install.verify("this-command-does-not-exist-xyz")
        self.assertFalse(ok)


class TestSettingsRoundTrip(unittest.TestCase):
    """Install and uninstall against a throwaway settings.json."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cache-timer-settings-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.settings = os.path.join(self.dir, "settings.json")
        self.wrapped = os.path.join(self.dir, "wrapped.json")
        self.patched = {
            "SETTINGS": install.SETTINGS,
            "WRAPPED": install.WRAPPED,
            "BACKUP_DIR": install.BACKUP_DIR,
        }
        install.SETTINGS = self.settings
        install.WRAPPED = self.wrapped
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
