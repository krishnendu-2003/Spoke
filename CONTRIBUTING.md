# Contributing to Spoke

Thanks for helping. Bug reports, fixes and small focused features are all welcome. For a
bigger change, open an issue first so we can agree on the approach before you spend time on it.

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md). Security problems go
through [private reporting](SECURITY.md), never a public issue.

## Set up

Python 3.11 or newer.

```bash
git clone https://github.com/krishnendu-2003/Spoke && cd Spoke
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install ruff pre-commit
.venv/bin/pre-commit install      # runs ruff and a secret scan before every commit
```

Linux also needs `libportaudio2`, and X11 needs `xclip` and `xdotool`.

## Naming conventions

Every change starts on its own branch off `main`, named for the kind of change it is. The
same type word leads the commit messages and the pull request title, so the history reads
the same way everywhere.

| Type | Use it for | Branch | Commit / PR title |
|---|---|---|---|
| `feat` | a new feature or user-visible behaviour | `feat/voice-commands` | `feat: add voice commands for punctuation` |
| `fix` | a bug fix | `fix/clipboard-restore-windows` | `fix: restore RTF clipboard on Windows` |
| `docs` | documentation only | `docs/wayland-setup` | `docs: explain the Wayland input group` |
| `test` | adding or fixing tests only | `test/hotkey-chords` | `test: cover Right Ctrl chord cancel` |
| `refactor` | code change with no behaviour change | `refactor/split-daemon` | `refactor: move paste logic out of daemon` |
| `perf` | a speed or memory improvement | `perf/warm-tls` | `perf: pre-open the Groq connection on key-down` |
| `ci` | GitHub Actions and build scripts | `ci/cache-pip` | `ci: cache pip downloads` |
| `chore` | dependencies, tooling, housekeeping | `chore/bump-httpx` | `chore(deps): bump httpx to 0.28.2` |

**Branch names** are `<type>/<short-description>`: lowercase, words joined with hyphens, no
spaces. If the change fixes an issue, you can put its number first, e.g. `fix/42-tray-crash`.

**Commit messages and PR titles** follow [Conventional Commits](https://www.conventionalcommits.org):
`<type>(<optional scope>): <summary>`.

- The summary is in the imperative ("add", not "added"), lowercase, with no full stop, and
  under 72 characters.
- An optional scope names the area: `hotkey`, `inject`, `voice`, `tray`, `app`, `config`,
  `deps`, and so on, e.g. `fix(inject): ...`.
- A breaking change (a config key renamed or removed, a changed default) adds `!` after the
  type, e.g. `feat(config)!: rename paste_method values`, and explains the migration in the body.
- Link the issue in the body or PR description: `Fixes #42`.

CI checks the branch name and the PR title and fails the `naming` check if either doesn't
follow these rules. Branches opened by Dependabot (`dependabot/...`) and Claude Code
(`claude/...`) are exempt from the branch rule, but their PR titles are still checked (Dependabot's
without the length limit).

## Before you open a pull request

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

CI runs the same checks on macOS, Windows and Linux, plus a secret scan of the whole git
history, a dependency vulnerability audit, and CodeQL. A pull request needs all of them green
and a review from the maintainer before it is merged.

- Keep each pull request to one change, and add or update tests for it. A bug fix comes with
  a test that fails without the fix.
- Don't commit API keys, `.env` files, recordings, `~/.spoke` contents or personal paths. If
  you commit a secret by accident, revoke it first; removing it from git is not enough.
- New dependencies must be pinned to an exact version in the right `requirements*.txt` file,
  with a short comment saying why they are needed.
- Behaviour or config changes need a matching README update.

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
