# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub: go to the repository's **Security** tab and click
**Report a vulnerability** ([direct link](https://github.com/krishnendu-2003/Spoke/security/advisories/new)).
Include what you found, how to reproduce it, and the impact you expect.

You can expect an acknowledgement within 5 working days and an assessment within 14 days.
Fixes are released as a new tagged version, and the advisory is published once users can
update. Credit is given in the advisory unless you ask otherwise.

## Supported versions

Only the latest release (and `main`) receives security fixes.

## What Spoke handles, and how

Spoke is a local desktop app, so most of its attack surface is on your own machine:

| Data | Where it lives | Leaves the machine? |
|---|---|---|
| Groq API key | OS keychain (service `spoke`), or the `GROQ_API_KEY` environment variable. Never in the config file or logs. | Only to `api.groq.com`, over HTTPS |
| Audio | In memory while you dictate. Written to `~/.spoke/debug_audio/` only if you turn on `debug_save_audio`. | Sent to Groq for transcription, unless `local_only = true` |
| Dictated text | `~/.spoke/history.jsonl` (turn off with `history = false`) | Sent to Groq for cleanup, unless `cleanup = false` or `local_only = true` |
| Voiceprint | `~/.spoke/voiceprint.json`, mode `0600` | Never |
| On-device models | `~/.spoke/models`, downloaded from the sherpa-onnx GitHub releases and verified against pinned SHA-256 hashes | Never |

`~/.spoke` is created with mode `0700`.

In scope: anything that leaks the API key, dictated text or audio beyond what the table
says; code execution through config, downloaded models or clipboard contents; the build and
release pipeline (`.github/workflows`, `packaging/`).

Out of scope: the fact that Spoke needs Accessibility / Input Monitoring permission to
work (it is a global hotkey and paste tool by design), and vulnerabilities in Groq's
service itself.

## Known limitations

- Spoke.app builds are ad-hoc signed, not notarized. Only install .dmg files from this
  repository's Releases page or its own CI runs.
- Anyone with access to your user account can read `~/.spoke`, like any other file you own.
