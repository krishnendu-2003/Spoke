# Spoke

Personal push-to-talk dictation. Hold a key anywhere, speak, release, and cleaned-up text is pasted into whatever has focus. Your clipboard is put back afterwards.

```
hold hotkey → record (16 kHz mono, in memory) → release → Groq Whisper (whisper-large-v3-turbo)
  → hallucination filter → LLM cleanup (punctuation, fillers, self-corrections only)
  → vocab replacements → paste into the focused app → restore clipboard
```

Target: under 1.5 s from key release to text on screen for a ~10 s utterance.

## Install (4 commands)

You need Python 3.11+ and a Groq API key (https://console.groq.com/keys).

**macOS / Linux**

```bash
git clone https://github.com/krishnendu-2003/Spoke && cd Spoke
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m spoke setup      # stores key in keychain, checks permissions, 3 s mic test
.venv/bin/python -m spoke            # run (tray icon if your desktop has a tray)
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/krishnendu-2003/Spoke; cd Spoke
py -3 -m venv .venv; .venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m spoke setup
.venv\Scripts\python -m spoke
```

**Linux system packages** (install these before the 4 commands):

| Session | Packages (Debian/Ubuntu names) |
|---|---|
| X11 | `sudo apt install libportaudio2 xclip xdotool` |
| Wayland | `sudo apt install libportaudio2 wl-clipboard wtype` (on GNOME also `ydotool`), then `sudo usermod -aG input $USER` and log out/in |

Optional offline STT: `.venv/bin/pip install -r requirements-local.txt` and set `local_only = true`.

Start at login: `scripts/install_autostart.sh` (macOS/Linux) or `powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1` (Windows).

## Usage

- **Hold** Right Option (macOS) / Right Ctrl (Windows, Linux), speak, release.
- **Toggle mode** (`mode = "toggle"`): double-tap to start, single tap to stop. Use it for long dictation.
- Holding the hotkey and pressing another key (e.g. Right Ctrl+C) cancels the recording. That was a shortcut, not dictation.
- Recordings under 0.3 s or quieter than `silence_rms_threshold` are dropped and never sent to the API.
- Recording auto-stops at `max_seconds` (default 5 min) with a notification, and what you said is still transcribed.
- Sounds: rising blip means recording, falling blip means processing, low double tone means error. Errors show as notifications and go to `~/.spoke/spoke.log`. An error message is never pasted into your document.

## Voice lock: only your voice, with background noise removed

```bash
.venv/bin/pip install -r requirements.txt   # adds sherpa-onnx (on-device models, ~12 MB)
.venv/bin/python -m spoke enroll            # downloads ~41 MB of models once, then 6 sentences, ~1 min
.venv/bin/python -m spoke test-mic --compare -v
```

`enroll` records you reading six sentences, saves a voiceprint to `~/.spoke/voiceprint.json` (`0600`), and turns on `voice_lock` and `noise_suppression`. From then on, each dictation is processed on your machine while you talk:

```
mic → noise suppression (GTCRN) → speech detection (Silero VAD) → ~1.5 s pieces
    → speaker match vs your voiceprint (TitaNet-small) → only your pieces go to Groq
```

- **Removed:** background noise, and other people talking before, after or between your sentences.
- **Not removed:** someone talking *at the same moment* as you. That piece is a mix; it's kept when you dominate it (you're closer to the mic) and cut when they do. Separating overlapping voices is target-speaker extraction, which needs a much heavier model and would blow the latency budget on CPU. A close mic (AirPods or a headset) is the practical fix for loud crowds.
- **If nothing sounds like you** (someone else held the key, or it's too loud), nothing is sent and you get a notification. If it keeps rejecting *you* somewhere, run `spoke enroll --add` there (adds 3 sentences recorded in that place) or lower `voice_lock_threshold`.
- **Cost:** about 0.1 s of CPU per second of audio on a cloud test CPU (your Mac should be faster), done while you talk; about 70-90 ms was left after key release there. The models load once at startup (~180 ms). `doctor` measures both on your machine.
- **Privacy:** audio and the voiceprint never leave the machine. The models come from the sherpa-onnx GitHub releases and are checked against pinned SHA-256 hashes. `rm ~/.spoke/voiceprint.json` deletes your voiceprint.

## CLI

| Command | What it does |
|---|---|
| `python -m spoke` | Run the daemon. Add `--debug` for per-stage latency in the log. |
| `python -m spoke setup [--reset-key] [--skip-mic]` | Store the Groq key in the OS keychain (validated against Groq), check permissions, run a mic test. |
| `python -m spoke test-mic [--seconds 3] [--force] [--compare] [-v]` | Record, then print duration, RMS vs threshold, what the voice filter kept (`-v`: each piece's score), raw and final transcript, and STT/cleanup latency. `--compare` also transcribes the unfiltered audio. |
| `python -m spoke enroll [--add] [--from-files a.wav ...] [--no-enable]` | Record your voiceprint for voice lock (see above). `--add` adds clips from a new place; `--download-only` just fetches the models. |
| `python -m spoke history [-n 20] [--stats]` | Recent transcripts. `--stats` gives median and max release→visible latency. |
| `python -m spoke doctor` | Checks deps, config, permissions, mic, key, Groq reachability (cold vs warm), and whether your STT and cleanup models are available. Also does a real STT and cleanup round trip with timings and lists the chat models your key can use. |

## Permissions you must grant by hand

**macOS.** Open System Settings > Privacy & Security and enable Spoke in each of these:

| Permission | Why |
|---|---|
| **Input Monitoring** | to see the hotkey |
| **Accessibility** | to send Cmd+V |
| **Microphone** | to record (macOS prompts on the first recording) |

The permission belongs to *the process that runs Python*. When you start Spoke from a terminal, that is **your terminal app** (Terminal, iTerm2, VS Code…). Under the LaunchAgent (autostart), it is **the real Python binary**, and `setup`/`doctor` print its exact path. In the settings pane, click +, press Cmd+Shift+G and paste the path. Quit and relaunch the terminal (or Spoke) after granting. `doctor` shows which of the three are missing.

**Windows.** No permission prompts. Windows blocks a normal process from sending keys to apps running *as Administrator* (UIPI), so dictation into elevated terminals or installers won't paste. Run Spoke elevated only if you need that.

**Linux.**
- **X11**: needs no permissions.
- **Wayland**: apps can't see global keys, so Spoke reads `/dev/input` through evdev and needs you in the `input` group (`sudo usermod -aG input $USER`, then re-login). That group can read every keystroke on the machine, which is the same power a keylogger has, so only do this on your own machine.

## Config reference (`~/.spoke/config.toml`)

The file is created with comments on first run. Restart Spoke after editing.

| Key | Default | Notes |
|---|---|---|
| `hotkey` | `"auto"` | Right Option on macOS, Right Ctrl elsewhere. Also `right_cmd`, `right_shift`, `caps_lock`, `f13`…`f20`, any pynput `Key` name |
| `mode` | `"hold"` | `hold` or `toggle` |
| `toggle_double_tap_ms` | `400` | double-tap window |
| `language` | `"en"` | ISO-639-1, or `"auto"` for mixed English/Hindi/Bengali |
| `stt_backend` | `"groq"` | `groq` or `local` (faster-whisper) |
| `stt_model` | `"whisper-large-v3-turbo"` | |
| `stt_timeout_seconds` | `10.0` | plus 1 s per 10 s of audio; one retry on timeout/5xx/429 |
| `upload_format` | `"flac"` | `flac` (lossless, ~1/3 smaller) or `wav` |
| `local_only` | `false` | forces local STT **and** disables cloud cleanup |
| `local_model_size` | `"base"` | `tiny`/`base`/`small`/`medium`/`large-v3` |
| `cleanup` | `true` | LLM cleanup pass |
| `cleanup_model` | `"auto"` | fastest model your key can use; a named model that isn't available falls back automatically |
| `cleanup_timeout_seconds` | `1.0` | slower means the raw transcript is used |
| `cleanup_min_words` | `4` | shorter utterances skip cleanup |
| `cleanup_mode` | `"smart"` | `smart` calls the LLM only when the text needs judgement (self-corrections, "you know", repeats, non-English) and strips plain "um/uh" locally; `always` sends everything |
| `sounds` | `true` | start/stop/error cues |
| `notifications` | `true` | desktop notifications for errors/auto-stop |
| `tray` | `true` | black-and-white waveform icon in the menu bar / tray: still bars when idle, bars swing with your voice while recording, a ripple while processing |
| `max_seconds` | `300` | auto-stop |
| `min_seconds` | `0.3` | shorter recordings are discarded |
| `silence_rms_threshold` | `150` | int16 RMS. Calibrate with `test-mic` |
| `keep_mic_open` | `false` | `true` makes start instant but keeps the OS mic indicator on |
| `input_device` | `""` | device name or index; empty means system default |
| `noise_suppression` | `false` | on-device noise removal before STT. `enroll` turns it on |
| `voice_lock` | `false` | send only speech that matches your voiceprint. `enroll` turns it on |
| `voice_lock_threshold` | `0` | `0` uses the threshold calibrated at enrollment (0.42-0.58). Raise it if other voices get through, lower it if your words get cut |
| `paste_method` | `"clipboard"` | `clipboard` (save, paste, restore) or `type` (simulated typing; never touches the clipboard) |
| `paste_restore_delay_ms` | `150` | wait before restoring the clipboard |
| `trailing_space` | `true` | space between consecutive dictations into the same app |
| `trailing_space_window_seconds` | `30` | |
| `linux_terminal_classes` | list | X11 window classes that get Ctrl+Shift+V |
| `history` | `true` | `~/.spoke/history.jsonl`, text only |
| `history_max_mb` | `5` | rotates to `.jsonl.1` |
| `log_level` | `"INFO"` | `DEBUG` logs per-stage latency |
| `debug_save_audio` | `false` | writes WAVs to `~/.spoke/debug_audio/` |
| `hallucination_blocklist` | list | exact whole-transcript matches; `re:` prefix for regex |
| `vocab` | seeded | Whisper prompt, highest priority first, auto-truncated to the 224-token limit |
| `[replacements]` | seeded | `"spoken form" = "Canonical"`, case-insensitive whole words, applied after cleanup |

The API key is **never** in this file. It is read from the OS keychain (service `spoke`), with `GROQ_API_KEY` as a fallback.

## Decisions

Each assumption made without asking, with its trade-off:

1. **FLAC for upload.** It is lossless, so accuracy is unchanged, and about a third smaller than WAV for real speech. Encoding 10 s takes about 2 ms. Upload time matters on a home uplink. Opus would be smaller still, but it took 120–280 ms to encode 10 s, which costs more than it saves. `upload_format = "wav"` is the fallback.
2. **Cleanup model is picked from what your key can use.** `cleanup_model = "auto"` tries `llama-3.1-8b-instant`, then `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`, `qwen/qwen3-32b`, `llama-3.3-70b-versatile` and `openai/gpt-oss-120b`, in that order. A named model that isn't available (or that Groq rejects mid-session) falls back to the same list rather than failing on every utterance. Model access varies by key: on one real key `llama-3.1-8b-instant` returned 404. Reasoning models are called with thinking turned down and hidden, following Groq's reasoning docs as of 2026-09-25: gpt-oss gets `reasoning_effort: "low"` and `include_reasoning: false`, and qwen3 gets `reasoning_effort: "none"` and `reasoning_format: "hidden"`. Spoke only ever reads `message.content`, and strips any inline `<think>` block. `doctor` times three cleanup calls against the 1 s budget.
3. **The Groq connection never goes cold.** A background keep-alive (one tiny `GET /models` after 20 s idle) holds the TLS connection open. A real Mac test showed a 3.7 s cold connect versus about 330 ms warm.
3b. **Smart cleanup.** Whisper already punctuates and capitalises, so most utterances only need "um"/"uh" removed. Spoke does that locally and skips the second Groq round trip. The LLM is still called for anything that needs judgement. `cleanup_mode = "always"` restores the old behaviour.
3c. **Warm TLS on key-down.** A persistent HTTP client (120 s keep-alive) pre-opens the Groq connection while you are still talking, which takes 100–300 ms of handshake out of the post-release path.
4. **Cleanup safety net.** Besides the strict prompt, the transcript is wrapped in `<transcript>` tags as data, and few-shot examples show a question being cleaned, not answered. If the output is much longer than the input (it answered) or much shorter (it summarised), Spoke pastes the raw transcript instead.
5. **"Trailing space" means a separator space before the next paste.** When you dictate into the same app within 30 s, the new text starts with a space. The result is the same as a trailing space, but a single dictation never leaves a dangling space. The rule is skipped if the text starts with punctuation.
6. **Hold-mode chord cancel.** Right Ctrl is also a real modifier, so pressing another key while holding it cancels the recording. Otherwise every Right-Ctrl shortcut would start a dictation.
7. **Hallucination blocklist is conservative.** It drops "Thanks for watching", a lone "you", and subtitle credits. It deliberately does *not* drop "Thank you." or "Okay.", because you might really dictate those. Silence is caught first by the RMS gate, so the blocklist is a second line of defence. Whisper can also read the vocab prompt back on unclear audio, so the prompt is a bare term list with no label word, and a transcript that is at least 75% vocab words (3+ words) is dropped as an echo.
8. **`stellar` is in `vocab` but not in `replacements`.** Otherwise "a stellar result" would become "a Stellar result". The same reasoning applies to other common English words, so add them only if Whisper keeps getting them wrong.
9. **Mic opens on key-down by default** (`keep_mic_open = false`). An always-open stream would keep the macOS/Windows mic indicator on all day. The time it takes to open the stream is logged at DEBUG. If it is over 50 ms on your machine, set `keep_mic_open = true`.
10. **Linux terminals.** Terminals paste with Ctrl+Shift+V, so Spoke reads the focused window's `WM_CLASS` (X11) and switches the chord. xterm/urxvt/st have no clipboard paste chord, so use `paste_method = "type"` for them.
11. **Linux autostart uses XDG autostart**, not a systemd user unit. It runs inside the graphical session, so the display variables and keyring are always there.
12. **Clipboard restore is best-effort.** macOS restores every type of every item. Windows restores all memory-backed formats (text, RTF, HTML, DIB images), but GDI-handle formats are lost. Linux restores the richest single target (e.g. `image/png` or `text/html`). If you copy something during the 150 ms paste window (detected on macOS/Windows), Spoke leaves your new copy alone.
13. **`~/.spoke` is created `0700`.** History holds your dictated text, so it stays private to your user.
14. **`local_only` has no local cleanup LLM.** It runs faster-whisper and then vocab replacements only. Running a local LLM on CPU would blow the latency budget.
15. **Voice lock judges each recording as a whole.** Your best-matching piece must clear the threshold, and then every piece within 0.20 of that best piece is kept. Noise drags all of *your* scores down together (about 0.8 in a quiet room, about 0.5 with crowd babble 10 dB below you), while another person's turn scores far below your best. A fixed cut-off can't serve both cases: in simulation it either dropped half your words in a café or let strangers through. `--compare` in `test-mic` shows whether noise suppression helps or hurts accuracy on your mic; turn it off with `noise_suppression = false` and voice lock still works.

## Uninstall / rollback

Everything lives in this repo and `~/.spoke/`, plus one autostart entry.

```bash
scripts/install_autostart.sh --uninstall        # macOS/Linux (Windows: install_autostart.ps1 -Uninstall)
.venv/bin/python -c "import keyring; keyring.delete_password('spoke', 'groq_api_key')"   # remove the API key
rm -rf ~/.spoke                                  # config, history, logs, voiceprint, voice models
rm -rf /path/to/Spoke                            # the repo + .venv
```

On macOS, also remove the terminal or Python entries you added under Privacy & Security if you want a clean slate.

## Known limitations

- **Wayland:** a global hotkey needs `input` group membership. Paste needs `wtype`, which works on wlroots (Sway, Hyprland) and KDE but not on GNOME, where `ydotool` plus its daemon is used instead. Spoke can't detect the focused app on Wayland, so terminals there need `paste_method = "type"`.
- **GNOME tray icon** needs the AppIndicator extension. Without it Spoke runs fine with no icon.
- **macOS Secure Input.** While a password field (or an app that enables Secure Input) has focus, macOS hides keystrokes from all listeners, so the hotkey won't fire there. That is by design.
- **macOS + tray.** pystray owns the main thread and pynput listens on a background thread. If you see a crash mentioning `TSMGetInputSourceProperty` or the main thread, set `tray = false`, and the listener then runs on the main thread.
- **Voice lock and overlapping speech:** see above. It removes other voices that aren't talking over you, not ones that are.
- **Groq bills STT at a 10 s minimum per request**, so very short dictations cost the same as 10 s ones. At $0.04/hour it is negligible, but it is real.
- Clipboard restore is lossy for exotic formats (see Decision 12).

## Next (out of scope for v1)

- Command mode: rewrite selected text by voice
- Per-app tone profiles
- Streaming partial transcripts
- Packaged `.app` / `.exe` (would also make macOS permissions attach to "Spoke" instead of Python)
- Snippets / text expansion

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
SPOKE_NATIVE_TESTS=1 .venv/bin/python -m pytest tests/test_clipboard_native.py   # real clipboard round trip (clobbers yours)
```

CI runs the tests on macOS, Windows and Linux (under Xvfb) with Python 3.11 and 3.13, including the real-clipboard test, and runs `doctor` for its informational output.
