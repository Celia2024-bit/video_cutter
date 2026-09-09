# PyInstaller spec. Run from the repo root:
#   pyinstaller packaging/VideoCutter.spec --noconfirm --clean

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

datas = [(str(ROOT / "webui"), "webui")]
binaries = []
hiddenimports = [
    "flask", "jinja2", "werkzeug", "click", "itsdangerous", "groq",
    "faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub", "av",
]

for pkg in ("faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub", "av", "flask"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hidden
    except Exception:
        hiddenimports += collect_submodules(pkg)

a = Analysis(
    [str(ROOT / "web.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "setuptools", "pkg_resources",
        "torch", "torchvision", "torchaudio", "tensorflow", "keras",
        "sklearn", "pandas", "pygame", "cv2", "nltk", "pyarrow",
        "IPython", "notebook", "jupyter", "scipy",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VideoCutter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=(sys.platform != "darwin"),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VideoCutter",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Video Cutter.app",
        icon=None,
        bundle_identifier="local.videocutter",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleName": "Video Cutter",
            "CFBundleDisplayName": "Video Cutter",
            "CFBundleShortVersionString": "1.0",
            "NSHumanReadableCopyright": "",
        },
    )
