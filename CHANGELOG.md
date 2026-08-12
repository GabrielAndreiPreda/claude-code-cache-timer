# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html).

Releases before 2.1.0 were tagged retroactively, so their dates come from the commits.

## [Unreleased]

Nothing yet.

## [2.1.0] - 2026-08-12

The encoding fix below was committed on 2026-08-10 but never released: `__version__` was
left at `2.0.0`, so no package reported itself as 2.1.0. This release is the first that
does.

### Fixed

- Install verification reported a working command as broken on Windows. It read the child
  process's output using the system's preferred encoding, which on a legacy code page
  cannot represent the hourglass. The failure happened inside a reader thread, so it
  neither propagated nor filled the buffer, and verification saw empty output. The reader
  now decodes UTF-8 explicitly, matching what the status line writes.
- `__version__` still read `2.0.0`.
- A test compared `candidate().argv[0]` against a literal POSIX path, but that value goes
  through `os.path.abspath`, which rewrites it on Windows. The test could not pass there.
- `test_real_repository` compared `git rev-parse --abbrev-ref HEAD` against `git_branch`,
  which cannot hold on a detached HEAD, where the former answers the literal string `HEAD`
  and the latter answers a short SHA. It now resolves the SHA, so the detached case is
  tested against real git rather than only a fixture.
- The README documented a status line wrapping mode that was removed in 2.0.0.
- `.claude/settings.local.json` is now ignored by the repository rather than relying on a
  contributor's global git configuration.

### Added

- Continuous integration on Linux, macOS and Windows across Python 3.8 through 3.13, plus a
  job that builds the package, checks its metadata, installs the wheel and runs the console
  script.
- `CONTRIBUTING.md`, issue and pull request templates, and this changelog.
- Package metadata: author, issue and changelog URLs, and per-version Python classifiers.
- A `--version` flag entry in the README options table, and instructions for pinning an
  install to a tag, since installing from `main` gave no way to hold a version.

### Changed

- The README is reorganised so the install command is reachable without scrolling, and the
  longer explanations of PATH, shell quoting and `settings.json` parsing moved into
  collapsible sections.
- `image.png` is now `docs/statusline.png`.

## [2.0.0] - 2026-08-10

### Changed

- Installation moved to `uv tool install` or `pipx install`, and the package gained a
  `claude-cache-timer` console script. The status line command written into
  `settings.json` is now that bare name.
- Shell detection is gone. A bare name has no path separators, no spaces and nothing any
  shell treats specially, so it runs identically whether Claude Code routes the status
  line through Git Bash, PowerShell or `sh`, and the installer never has to work out
  which.
- The code moved to a `src/cache_timer/` package with separate `cli`, `statusline` and
  `install` modules. `install.py` and `uninstall.py` at the repository root were replaced
  by `claude-cache-timer install` and `claude-cache-timer uninstall`.

### Removed

- Status line wrapping. Earlier versions kept an existing status line and appended the
  countdown to its output, which meant running the wrapped command under `/bin/sh` or
  `cmd.exe`, often not the shell it was written for. The installer now stops when it finds
  a status line it did not write, and `--force` replaces it.

## [1.0.0] - 2026-08-07

### Added

- Initial release. A status line segment counting down the session's prompt cache, reading
  both the remaining time and the cache TTL out of the session transcript, with no hooks
  and no state file.

[Unreleased]: https://github.com/GabrielAndreiPreda/claude-code-cache-timer/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/GabrielAndreiPreda/claude-code-cache-timer/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/GabrielAndreiPreda/claude-code-cache-timer/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/GabrielAndreiPreda/claude-code-cache-timer/releases/tag/v1.0.0
