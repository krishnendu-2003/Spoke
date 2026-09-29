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

## Before you open a pull request

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

CI runs the same checks on macOS, Windows and Linux, plus a secret scan of the whole git
history, a dependency vulnerability audit, and CodeQL. A pull request needs all of them green
and a review from the maintainer before it is merged.

- Keep each pull request to one change, and add or update tests for it.
- Don't commit API keys, `.env` files, recordings, `~/.spoke` contents or personal paths. If
  you commit a secret by accident, revoke it first; removing it from git is not enough.
- New dependencies must be pinned to an exact version in the right `requirements*.txt` file,
  with a short comment saying why they are needed.
- Behaviour or config changes need a matching README update.

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
