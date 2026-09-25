"""CLI commands: run (default), setup, test-mic, history, doctor."""

from __future__ import annotations

import argparse
import getpass
import importlib
import statistics
import sys
import time

from . import __version__
from . import config as config_mod
from .config import Config, config_path, get_api_key, set_api_key, spoke_home
from .platform_info import CURRENT


def _load_cfg() -> Config:
    try:
        return config_mod.load()
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(2)


# --- run -----------------------------------------------------------------------------------

def cmd_run(args) -> int:
    from .daemon import Daemon, setup_logging
    from .permissions import format_checks, run_checks

    cfg = _load_cfg()
    if args.debug:
        cfg.log_level = "DEBUG"
    setup_logging(cfg)
    key, _ = get_api_key()
    if cfg.effective_stt_backend == "groq" and not key:
        print("No Groq API key found. Run `python -m spoke setup` (or set GROQ_API_KEY).", file=sys.stderr)
        return 2
    bad = [c for c in run_checks() if c.ok is False]
    if bad:
        print("Spoke may not work until these are fixed:\n" + format_checks(bad), file=sys.stderr)
    try:
        Daemon(cfg).run()
    except ValueError as e:  # bad hotkey name etc.
        print(f"Error: {e}", file=sys.stderr)
        return 2
    return 0


# --- test-mic ------------------------------------------------------------------------------

def cmd_test_mic(args) -> int:
    from .daemon import Pipeline, setup_logging
    from .groq_api import GroqClient
    from .recorder import Recorder, rejection_reason
    from .transcribe import TranscriptionError

    cfg = _load_cfg()
    setup_logging(cfg, console=args.verbose)
    seconds = args.seconds
    rec_ = Recorder(max_seconds=seconds + 1, device=cfg.input_device)
    print(f"Recording {seconds:.0f} s from the default mic... speak now.")
    try:
        rec = rec_.record_for(seconds)
    except Exception as e:
        print(f"Microphone error: {type(e).__name__}: {e}")
        if CURRENT.is_mac:
            print("Check System Settings > Privacy & Security > Microphone for your terminal app.")
        return 1
    print(f"  duration:  {rec.duration:.2f} s   (mic stream opened in {rec_.last_open_ms:.0f} ms)")
    print(f"  RMS level: {rec.rms:.0f}   (silence threshold: {cfg.silence_rms_threshold:.0f})")
    reason = rejection_reason(rec, cfg.min_seconds, cfg.silence_rms_threshold)
    if reason:
        print(f"  -> would be discarded: {reason}")
        print("     If you were speaking, lower silence_rms_threshold in config.toml "
              f"(e.g. to {max(10, rec.rms * 0.5):.0f}).")
        if not args.force:
            return 1
    key, _ = get_api_key()
    client = GroqClient(key) if key else None
    try:
        pipe = Pipeline(cfg, client, injector=None, history=None)
        t0 = time.perf_counter()
        text, info = pipe.text_for(rec)
        total = (time.perf_counter() - t0) * 1000
    except TranscriptionError as e:
        print(f"Transcription failed: {e}")
        return 1
    print(f"  raw:       {info['raw']!r}")
    print(f"  final:     {text!r}")
    print(f"  latency:   stt {info['stt_ms']:.0f} ms | cleanup {info['cleanup_ms']:.0f} ms ({info['cleanup']}) "
          f"| total {total:.0f} ms (excl. paste)")
    return 0


# --- history -------------------------------------------------------------------------------

def cmd_history(args) -> int:
    from .history import History

    h = History(spoke_home() / "history.jsonl")
    entries = h.recent(args.n)
    if not entries:
        print("No history yet.")
        return 0
    for e in entries:
        lat = e.get("latency") or {}
        total = f"{lat.get('total_ms', 0):.0f}ms" if lat else ""
        print(f"{e.get('ts', '')}  [{e.get('app') or '?'}] {total}")
        print(f"  {e.get('text', '')}")
    if args.stats:
        totals = [e["latency"]["total_ms"] for e in entries if e.get("latency")]
        stt = [e["latency"]["stt_ms"] for e in entries if e.get("latency")]
        if totals:
            print(f"\nrelease->pasted over {len(totals)}: median {statistics.median(totals):.0f} ms, "
                  f"max {max(totals):.0f} ms; STT median {statistics.median(stt):.0f} ms")
    return 0


# --- setup ---------------------------------------------------------------------------------

def cmd_setup(args) -> int:
    from .groq_api import GroqClient, GroqError
    from .permissions import format_checks, mac_request_prompts, run_checks

    print(f"Spoke {__version__} setup on {CURRENT.describe()}\n")
    cfg = _load_cfg()
    print(f"Config: {config_path()}")

    key, source = get_api_key()
    if key and not args.reset_key:
        print(f"Groq API key: found ({source}). Use `setup --reset-key` to replace it.")
    elif cfg.local_only:
        print("local_only = true: no API key needed.")
    else:
        print("Paste your Groq API key (https://console.groq.com/keys). Input is hidden.")
        new = getpass.getpass("GROQ_API_KEY: ").strip()
        if not new:
            print("No key entered.")
            return 1
        try:
            models = GroqClient(new).list_models()
            print(f"Key works ({len(models)} models available).")
        except GroqError as e:
            print(f"Groq rejected the key: {e}")
            return 1
        except Exception as e:
            print(f"Couldn't reach Groq to verify ({type(e).__name__}); storing anyway.")
        try:
            set_api_key(new)
            print("Stored in the OS keychain (service 'spoke').")
        except Exception as e:
            print(f"Couldn't use the OS keychain ({type(e).__name__}: {e}).")
            print("Set it as an environment variable instead, e.g. in your shell profile:")
            print("  export GROQ_API_KEY=...        (macOS/Linux)")
            print("  setx GROQ_API_KEY \"...\"        (Windows)")

    print("\nPermissions:")
    if CURRENT.is_mac:
        mac_request_prompts()
    print(format_checks(run_checks()))

    if not args.skip_mic:
        print("\nMic test:")
        args.seconds, args.force, args.verbose = 3.0, False, False
        cmd_test_mic(args)
    print("\nDone. Start Spoke with:  python -m spoke")
    return 0


# --- doctor --------------------------------------------------------------------------------

def _line(status: str, name: str, detail: str = "") -> None:
    print(f"  [{status:<4}] {name}" + (f": {detail}" if detail else ""))


def cmd_doctor(args) -> int:
    from .permissions import format_checks, run_checks

    failures = 0
    print(f"Spoke {__version__} doctor\n")
    print("Environment")
    py = sys.version_info
    ok = py >= (3, 11)
    failures += not ok
    _line("OK" if ok else "FAIL", "Python", f"{py.major}.{py.minor}.{py.micro} ({sys.executable})")
    _line("OK", "Platform", CURRENT.describe() or "unknown")
    if CURRENT.is_linux and not CURRENT.session:
        _line("FAIL", "Display session", "XDG_SESSION_TYPE / DISPLAY / WAYLAND_DISPLAY not set (headless?)")
        failures += 1

    print("\nDependencies")
    cfg = _load_cfg()
    mods = ["numpy", "httpx", "keyring", "pynput", "sounddevice", "pystray", "PIL"]
    if CURRENT.is_mac:
        mods += ["Quartz", "AppKit", "ApplicationServices", "AVFoundation"]
    if cfg.effective_stt_backend == "local":
        mods.append("faster_whisper")
    for m in mods:
        try:
            mod = importlib.import_module(m)
            _line("OK", m, getattr(mod, "__version__", ""))
        except Exception as e:
            _line("FAIL", m, f"{type(e).__name__}: {str(e).splitlines()[0][:120]}")
            failures += 1

    print("\nConfig")
    _line("OK", "file", str(config_path()))
    try:
        from .hotkey import resolve_hotkey

        _line("OK", "hotkey", f"{cfg.hotkey} -> {resolve_hotkey(cfg.hotkey)} ({cfg.mode} mode)")
    except Exception as e:
        _line("FAIL", "hotkey", str(e).splitlines()[0][:160])
        failures += 1
    _line("OK", "stt", f"{cfg.effective_stt_backend} / language={cfg.language}"
          + (" (local_only)" if cfg.local_only else ""))
    _line("OK", "cleanup", f"{cfg.cleanup_model}" if cfg.effective_cleanup else "off")

    print("\nPermissions")
    checks = run_checks()
    print(format_checks(checks))
    failures += sum(1 for c in checks if c.ok is False)

    print("\nMicrophone")
    try:
        import sounddevice as sd

        dev = sd.query_devices(kind="input")
        _line("OK", "default input", f"{dev['name']} ({int(dev['default_samplerate'])} Hz native)")
    except Exception as e:
        _line("FAIL", "default input", f"{type(e).__name__}: {str(e).splitlines()[0][:120]}")
        failures += 1

    print("\nGroq")
    key, source = get_api_key()
    needs_cloud = cfg.effective_stt_backend == "groq" or cfg.effective_cleanup
    if not key:
        _line("FAIL" if needs_cloud else "SKIP", "API key", "missing -- run `python -m spoke setup`")
        failures += needs_cloud
    else:
        _line("OK", "API key", f"found in {source}")
        failures += _doctor_groq(cfg, key)

    print(f"\n{'All checks passed.' if not failures else f'{failures} problem(s) found.'}")
    return 1 if failures else 0


def _doctor_groq(cfg: Config, key: str) -> int:
    import numpy as np

    from . import cleanup as cleanup_mod
    from .groq_api import GroqClient, GroqError
    from .recorder import Recording

    failures = 0
    client = GroqClient(key)
    try:
        times = []
        models: list[str] = []
        for _ in range(3):
            t0 = time.perf_counter()
            models = client.list_models()
            times.append((time.perf_counter() - t0) * 1000)
        _line("OK", "reachable", f"GET /models first {times[0]:.0f} ms (cold TLS), warm median "
              f"{statistics.median(times[1:]):.0f} ms")
    except Exception as e:
        _line("FAIL", "reachable", f"{type(e).__name__}: {e}")
        return 1

    for label, model in (("stt model", cfg.stt_model), ("cleanup model", cfg.cleanup_model)):
        if model in models:
            _line("OK", label, model)
        else:
            _line("FAIL", label, f"{model} not available to this key")
            failures += 1
    chat_models = [m for m in models if not any(x in m for x in ("whisper", "guard", "tts", "playai", "orpheus", "distil"))]
    print(f"         chat models your key can use: {', '.join(chat_models)}")

    # Real round trips (tiny cost: Groq bills STT at a 10 s minimum per request).
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(16000 * 2) * 300).astype(np.int16)
    try:
        t0 = time.perf_counter()
        client.transcribe(Recording(noise).to_wav(), model=cfg.stt_model, language="en",
                          prompt="", timeout=cfg.stt_timeout_seconds)
        _line("OK", "STT round trip", f"{(time.perf_counter() - t0) * 1000:.0f} ms for 2 s of audio")
    except GroqError as e:
        _line("FAIL", "STT round trip", str(e))
        failures += 1
    if cfg.effective_cleanup:
        sample = "um so the deploy is at 5 no 6 tomorrow you know"
        t0 = time.perf_counter()
        out, status = cleanup_mod.clean(sample, client, model=cfg.cleanup_model,
                                        timeout=cfg.cleanup_timeout_seconds)
        ms = (time.perf_counter() - t0) * 1000
        ok = status == "ok"
        _line("OK" if ok else "FAIL", "cleanup round trip", f"{ms:.0f} ms ({status}) -> {out!r}")
        failures += not ok
    return failures


# --- entry ---------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m spoke", description="Push-to-talk dictation.")
    p.add_argument("--version", action="version", version=f"spoke {__version__}")
    p.add_argument("--debug", action="store_true", help="DEBUG logging (per-stage latency)")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("setup", help="store API key, check permissions, test mic")
    s.add_argument("--reset-key", action="store_true")
    s.add_argument("--skip-mic", action="store_true")
    s.set_defaults(func=cmd_setup)

    t = sub.add_parser("test-mic", help="record a few seconds, print RMS and transcript")
    t.add_argument("--seconds", type=float, default=3.0)
    t.add_argument("--force", action="store_true", help="transcribe even if it looks silent")
    t.add_argument("-v", "--verbose", action="store_true")
    t.set_defaults(func=cmd_test_mic)

    h = sub.add_parser("history", help="print recent transcripts")
    h.add_argument("-n", type=int, default=20)
    h.add_argument("--stats", action="store_true", help="latency summary")
    h.set_defaults(func=cmd_history)

    d = sub.add_parser("doctor", help="check deps, permissions, key, Groq reachability, latency")
    d.set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    if not args.cmd:
        return cmd_run(args)
    return args.func(args)
