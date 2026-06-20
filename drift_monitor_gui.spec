# -*- mode: python ; coding: utf-8 -*-
# HP 3458A Drift Monitor – PyInstaller build spec
#
# Build:
#   cd C:\Users\Mate\Desktop\teszt\GPIB
#   C:\Python311\python.exe -m PyInstaller drift_monitor_gui.spec

a = Analysis(
    ['drift_monitor_gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['tkinter', 'tkinter.ttk', 'tkinter.font'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'pandas', 'matplotlib', 'scipy', 'PIL',
              'uvicorn', 'fastapi', 'starlette'],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='HP3458A_DriftMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # Nincs konzolablak – csak a GUI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
