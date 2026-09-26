# PyInstaller spec for Spoke.app. Build with packaging/macos/build.sh (not by hand).
# One-folder bundle: Python, numpy, sherpa-onnx (+ onnxruntime), PortAudio, libsndfile and
# PyObjC all live inside Spoke.app, so nothing needs to be installed on the Mac.
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
sys.path.insert(0, ROOT)
from spoke import __version__  # noqa: E402
from spoke.app import BUNDLE_ID  # noqa: E402

ICON = os.environ.get("SPOKE_ICON", os.path.join(ROOT, "build", "Spoke.icns"))

datas, binaries, hiddenimports = [], [], []
# sherpa-onnx loads libonnxruntime.dylib from its own lib/ folder via @loader_path.
for pkg in ("sherpa_onnx",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
# pynput, pystray and keyring pick their backend at runtime by module name.
hiddenimports += [
    "pynput.keyboard._darwin",
    "pynput.mouse._darwin",
    "pynput._util.darwin",
    "pystray._darwin",
    "AppKit",
    "Foundation",
    "Quartz",
    "ApplicationServices",
    "AVFoundation",
]
hiddenimports += collect_submodules("keyring.backends")
hiddenimports += collect_submodules("spoke")
datas += copy_metadata("keyring")  # keyring discovers backends through entry points

a = Analysis(
    [os.path.join(SPECPATH, "spoke_app.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "faster_whisper", "ctranslate2", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Spoke",
    console=False,
    argv_emulation=False,
    codesign_identity=None,  # ad-hoc; build.sh re-signs the whole bundle
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Spoke")
app = BUNDLE(
    coll,
    name="Spoke.app",
    icon=ICON,
    bundle_identifier=BUNDLE_ID,
    version=__version__,
    info_plist={
        "CFBundleName": "Spoke",
        "CFBundleDisplayName": "Spoke",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSUIElement": True,  # menu-bar app: no Dock icon, no app menu
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        # Without this string macOS kills the app the first time it opens the mic.
        "NSMicrophoneUsageDescription": "Spoke records while you hold the dictation key and turns your speech into text.",
        "NSHumanReadableCopyright": "Personal build",
    },
)
