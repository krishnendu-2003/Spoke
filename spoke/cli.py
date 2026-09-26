"""CLI commands: run (default), setup, enroll, test-mic, history, doctor."""

from __future__ import annotations

import argparse
import getpass
import importlib
import statistics
import sys
import time

from . import __version__
from . import config as config_mod
from .config import Config, config_path, get_api_key, set_api_key, spoke_cmd, spoke_home
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
        print(f"No Groq API key found. Run `{spoke_cmd('setup')}` (or set GROQ_API_KEY).", file=sys.stderr)
        return 2
    from . import instance

    lock = instance.acquire()  # noqa: F841 -- held for the life of the process
    if lock is None:
        pid = instance.holder_pid()
        print(f"Spoke is already running{f' (pid {pid})' if pid else ''}. Quit it first: two copies "
              "would paste every dictation twice.", file=sys.stderr)
        return 1
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
    from .voice import VoiceFilter

    vf = VoiceFilter(cfg)
    silence = 0.0 if vf.active else cfg.silence_rms_threshold
    gate = "speech detection (voice filter on)" if vf.active else f"silence threshold: {silence:.0f}"
    print(f"  RMS level: {rec.rms:.0f}   ({gate})")
    reason = rejection_reason(rec, cfg.min_seconds, silence)
    if reason:
        print(f"  -> would be discarded: {reason}")
        print("     If you were speaking, lower silence_rms_threshold in config.toml "
              f"(e.g. to {max(10, rec.rms * 0.5):.0f}).")
        if not args.force:
            return 1
    key, _ = get_api_key()
    client = GroqClient(key) if key else None
    try:
        pipe = Pipeline(cfg, client, injector=None, history=None, voice=vf)
        if pipe.voice.problem:
            print(f"  voice:     off -- {pipe.voice.problem}")
        original = rec
        if pipe.voice.active:
            rec, vinfo = pipe.apply_voice(rec)
            res_line = vinfo.get("voice", "?")
            print(f"  voice:     {res_line}; sending {vinfo.get('voice_kept_s', 0):.1f} s "
                  f"(threshold {pipe.voice.threshold:.2f})")
            if args.verbose:
                print(f"             {vinfo.get('voice_detail', '')}")
            if rec is None:
                print("  -> nothing sent: " + ("no speech found" if res_line == "no-speech"
                                               else "no speech matched your voiceprint"))
                return 1
        pipe.resolve_cleanup_model()
        t0 = time.perf_counter()
        text, info = pipe.text_for(rec)
        total = (time.perf_counter() - t0) * 1000
        if args.compare and rec is not original:
            before, _ = pipe.text_for(original)
            print(f"  unfiltered:{before!r}")
    except TranscriptionError as e:
        print(f"Transcription failed: {e}")
        return 1
    print(f"  raw:       {info['raw']!r}")
    print(f"  final:     {text!r}")
    print(f"  latency:   stt {info['stt_ms']:.0f} ms | cleanup {info['cleanup_ms']:.0f} ms "
          f"({info['cleanup']}, {info.get('cleanup_model') or 'none'}) "
          f"| total {total:.0f} ms (excl. paste)")
    return 0


# --- enroll --------------------------------------------------------------------------------

def cmd_enroll(args) -> int:
    from . import voice
    from .recorder import Recorder

    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        print(f"Voice lock needs sherpa-onnx: {sys.executable} -m pip install -r requirements.txt")
        return 1
    cfg = _load_cfg()
    try:
        voice.download_models()
    except Exception as e:
        print(f"Couldn't download the voice models: {type(e).__name__}: {e}")
        return 1
    if args.download_only:
        print(f"Models ready in {voice.models_dir()}")
        return 0
    engine = voice.Engine()
    base = None
    if args.add:
        try:
            base = voice.Profile.load()
        except Exception as e:
            print(f"Existing voiceprint unreadable ({e}); starting over.")
        if base is None:
            print("No voiceprint yet; doing a full enrollment.")

    clips: list = []
    if args.files:
        import soundfile as sf

        for f in args.files:
            x, sr = sf.read(f, dtype="int16", always_2d=True)
            clip = voice.analyse_clip(engine, voice.resample(x.mean(axis=1).astype("int16"), sr))
            print(f"  {f}: {clip.speech_s:.1f} s of speech")
            if clip.speech_s >= 1.0:
                clips.append(clip)
    else:
        n = args.clips or (3 if base else 6)
        print("Voice enrollment: read each sentence aloud in your normal dictation voice, with the")
        print("mic you normally use. A quiet room is best for the first enrollment; later, run")
        print(f"`{spoke_cmd('enroll --add')}` somewhere noisy to teach it that too.\n")
        rec_ = Recorder(max_seconds=args.seconds + 1, device=cfg.input_device)
        prompts = voice.ENROLL_PROMPTS
        i = 0
        while len(clips) < n:
            line = prompts[(i + (len(base.embeddings) if base else 0)) % len(prompts)]
            i += 1
            print(f"[{len(clips) + 1}/{n}]  \"{line}\"")
            try:
                input(f"        Press Enter, then read it ({args.seconds:.0f} s)...")
            except (EOFError, KeyboardInterrupt):
                print("\nCancelled; nothing saved.")
                return 1
            try:
                rec = rec_.record_for(args.seconds)
            except Exception as e:
                print(f"Microphone error: {type(e).__name__}: {e}")
                return 1
            clip = voice.analyse_clip(engine, rec.samples)
            if clip.speech_s < 2.0:
                print(f"        Only caught {clip.speech_s:.1f} s of speech (RMS {rec.rms:.0f}). Let's redo that one.")
                if i > n * 2 + 2:
                    print("Too many retries; check the mic with `test-mic`.")
                    return 1
                continue
            print(f"        ok, {clip.speech_s:.1f} s of speech")
            clips.append(clip)

    total = len(clips) + (len(base.embeddings) if base else 0)
    if total < 3:
        print(f"Need at least 3 usable clips, got {total}. Nothing saved.")
        return 1
    profile = voice.build_profile(engine, clips, base)
    path = profile.save()
    import statistics

    med = statistics.median(profile.self_scores) if profile.self_scores else float("nan")
    print(f"\nSaved your voiceprint ({len(profile.embeddings)} clips) to {path}")
    print(f"Your own speech scores a median {med:.2f} against it; threshold set to {profile.threshold:.2f}.")
    if not args.no_enable:
        config_mod.set_values({"voice_lock": True})
        print("Turned on voice_lock in config.toml. Restart Spoke to apply.")
    print(f"Try it:  {spoke_cmd('test-mic --compare -v')}")
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
        args.seconds, args.force, args.verbose, args.compare = 3.0, False, False, False
        cmd_test_mic(args)
    print(f"\nDone. Start Spoke with:  {spoke_cmd()}")
    print(f"Check everything with:  {spoke_cmd('doctor')}")
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

    failures += _doctor_voice(cfg)

    print("\nGroq")
    key, source = get_api_key()
    needs_cloud = cfg.effective_stt_backend == "groq" or cfg.effective_cleanup
    if not key:
        _line("FAIL" if needs_cloud else "SKIP", "API key", f"missing -- run `{spoke_cmd('setup')}`")
        failures += needs_cloud
    else:
        _line("OK", "API key", f"found in {source}")
        failures += _doctor_groq(cfg, key, bench=getattr(args, "bench", False))

    print(f"\n{'All checks passed.' if not failures else f'{failures} problem(s) found.'}")
    return 1 if failures else 0


def _doctor_voice(cfg: Config) -> int:
    from . import voice

    print("\nVoice lock (on-device)")
    wanted = cfg.voice_lock or cfg.noise_suppression
    bad = "FAIL" if wanted else "SKIP"
    failures = 0
    try:
        import sherpa_onnx

        _line("OK", "sherpa-onnx", getattr(sherpa_onnx, "__version__", ""))
    except Exception as e:
        _line(bad, "sherpa-onnx", f"{type(e).__name__}: {str(e).splitlines()[0][:120]}")
        return int(wanted)
    missing = voice.missing_models()
    if missing:
        _line(bad, "models", f"missing {', '.join(m.filename for m in missing)} -- run `{spoke_cmd('enroll')}`")
        failures += wanted
    else:
        _line("OK", "models", str(voice.models_dir()))
    try:
        prof = voice.Profile.load()
    except Exception as e:
        prof = None
        _line("FAIL", "voiceprint", str(e))
        failures += 1
    else:
        if prof:
            _line("OK", "voiceprint", f"{len(prof.embeddings)} clips, threshold {prof.threshold:.2f} ({voice.profile_path()})")
        else:
            _line("FAIL" if cfg.voice_lock else "SKIP", "voiceprint", f"not enrolled -- run `{spoke_cmd('enroll')}`")
            failures += cfg.voice_lock
    th = f", threshold override {cfg.voice_lock_threshold}" if cfg.voice_lock_threshold else ""
    _line("OK", "settings", f"voice_lock={str(cfg.voice_lock).lower()}, "
          f"noise_suppression={str(cfg.noise_suppression).lower()}{th}")
    if wanted and not missing:
        failures += _doctor_voice_bench()
    return failures


def _doctor_voice_bench() -> int:
    """Time the filter on synthetic audio: load, and per-second streaming cost."""
    import numpy as np

    from . import voice

    try:
        t0 = time.perf_counter()
        engine = voice.Engine()
        load_ms = (time.perf_counter() - t0) * 1000
        rng = np.random.default_rng(0)
        audio = (rng.standard_normal(16000 * 5) * 300).astype(np.int16)
        s = voice.VoiceSession(engine, voice._unit(np.ones(engine.dim, np.float32)), 0.5)
        t0 = time.perf_counter()
        voice.feed_all(s, audio)
        s.finish()
        per_s = (time.perf_counter() - t0) * 1000 / 5
        _line("OK", "speed", f"models load {load_ms:.0f} ms (once, at startup); {per_s:.0f} ms of work per second "
              "of audio, done while you talk")
        return 0
    except Exception as e:
        _line("FAIL", "speed", f"{type(e).__name__}: {e}")
        return 1


def _doctor_groq(cfg: Config, key: str, bench: bool = False) -> int:
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

    if cfg.stt_model in models:
        _line("OK", "stt model", cfg.stt_model)
    else:
        _line("FAIL", "stt model", f"{cfg.stt_model} not available to this key")
        failures += 1
    cleanup_model = cleanup_mod.pick_cleanup_model(cfg.cleanup_model, models)
    reasoning = " (reasoning model: thinking set low/off and hidden)" if cleanup_mod.is_reasoning_model(cleanup_model or "") else ""
    if cleanup_model is None:
        _line("FAIL", "cleanup model", "no usable chat model for this key")
        failures += 1
    elif cleanup_model == cfg.cleanup_model:
        _line("OK", "cleanup model", cleanup_model + reasoning)
    else:
        why = "auto" if cfg.cleanup_model == "auto" else f"{cfg.cleanup_model} not available to this key"
        _line("OK", "cleanup model", f"{cleanup_model}{reasoning} ({why}; picked automatically)")
    chat_models = [m for m in models if not any(x in m for x in ("whisper", "guard", "tts", "playai", "orpheus", "distil"))]
    print(f"         chat models your key can use: {', '.join(chat_models)}")

    # Real round trips (tiny cost: Groq bills STT at a 10 s minimum per request).
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(16000 * 2) * 300).astype(np.int16)
    try:
        t0 = time.perf_counter()
        audio, fname, mime = Recording(noise).to_upload(cfg.upload_format)
        client.transcribe(audio, filename=fname, mime=mime, model=cfg.stt_model, language="en",
                          prompt="", timeout=cfg.stt_timeout_seconds)
        _line("OK", "STT round trip", f"{(time.perf_counter() - t0) * 1000:.0f} ms for 2 s of audio ({fname})")
    except GroqError as e:
        _line("FAIL", "STT round trip", str(e))
        failures += 1
    if cfg.effective_cleanup and cleanup_model:
        sample = "um so the deploy is at 5 no 6 tomorrow you know"
        runs = []
        for _ in range(3):  # first call can include connection/model warm-up
            t0 = time.perf_counter()
            out, status = cleanup_mod.clean(sample, client, model=cleanup_model,
                                            timeout=max(cfg.cleanup_timeout_seconds, 5.0))
            runs.append(((time.perf_counter() - t0) * 1000, status, out))
        ok = all(s == "ok" for _, s, _ in runs)
        med = statistics.median(ms for ms, _, _ in runs)
        within = med <= cfg.cleanup_timeout_seconds * 1000
        _line("OK" if ok and within else "FAIL", "cleanup round trip",
              f"median {med:.0f} ms of {', '.join(f'{ms:.0f}' for ms, _, _ in runs)} "
              f"(budget {cfg.cleanup_timeout_seconds * 1000:.0f} ms, {runs[-1][1]}) -> {runs[-1][2]!r}")
        if ok and not within:
            print("         over budget: dictation would fall back to raw text. Try another model or raise "
                  "cleanup_timeout_seconds.")
        failures += not (ok and within)
    if bench:
        _bench_cleanup(cfg, client, models)
    return failures


def _bench_cleanup(cfg: Config, client, models: list[str]) -> None:
    """Time every preferred cleanup model this key can use; recommend the fastest that works."""
    from . import cleanup as cleanup_mod

    sample = "um so the deploy is at 5 no 6 tomorrow and uh ping me on slack you know"
    print("\nCleanup model benchmark (5 calls each, same prompt Spoke uses)")
    results = []
    for m in [m for m in cleanup_mod.PREFERRED_CLEANUP_MODELS if m in models]:
        times, statuses, out = [], [], ""
        for _ in range(5):
            t0 = time.perf_counter()
            out, status = cleanup_mod.clean(sample, client, model=m, timeout=5.0)
            times.append((time.perf_counter() - t0) * 1000)
            statuses.append(status)
        ok = all(s == "ok" for s in statuses)
        med = statistics.median(times[1:])  # first call can include warm-up
        results.append((med, m, ok))
        _line("OK" if ok else "FAIL", m, f"median {med:.0f} ms -> {out!r}")
    good = sorted(r for r in results if r[2])
    if good:
        print(f"  fastest: {good[0][1]} ({good[0][0]:.0f} ms). To use it: cleanup_model = \"{good[0][1]}\"")


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
    t.add_argument("--compare", action="store_true",
                   help="also transcribe the unfiltered audio (voice lock / noise suppression A/B)")
    t.set_defaults(func=cmd_test_mic)

    e = sub.add_parser("enroll", help="record your voice once so voice lock only takes your speech")
    e.add_argument("--add", action="store_true", help="add clips to your existing voiceprint (e.g. in a noisy place)")
    e.add_argument("--clips", type=int, default=0, help="how many sentences to record (default 6, or 3 with --add)")
    e.add_argument("--seconds", type=float, default=6.0, help="seconds per sentence")
    e.add_argument("--from-files", dest="files", nargs="+", metavar="WAV", help="enroll from recordings of you instead")
    e.add_argument("--no-enable", action="store_true", help="save the voiceprint but don't turn voice lock on")
    e.add_argument("--download-only", action="store_true", help="just fetch the on-device models")
    e.set_defaults(func=cmd_enroll)

    h = sub.add_parser("history", help="print recent transcripts")
    h.add_argument("-n", type=int, default=20)
    h.add_argument("--stats", action="store_true", help="latency summary")
    h.set_defaults(func=cmd_history)

    d = sub.add_parser("doctor", help="check deps, permissions, key, Groq reachability, latency")
    d.add_argument("--bench", action="store_true", help="also time every cleanup model your key can use")
    d.set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    if not args.cmd:
        return cmd_run(args)
    return args.func(args)
