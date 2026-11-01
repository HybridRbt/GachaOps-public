# Windows Qt uses the OS ICU shim. Do not bundle a developer tool's incompatible
# icuuc.dll (e.g. Poppler exports version-suffixed ICU symbols instead).
from pathlib import Path

root = Path(SPECPATH)
a = Analysis(
    [str(root / 'scripts/desktop_entry.py')],
    pathex=[str(root), str(root / 'build/gui-deps')],
    binaries=[], datas=[(str(root / 'gachaops/onedragon_bridge.py'), 'gachaops'),
                        (str(root / 'gachaops/assets/support_matrix.json'), 'gachaops/assets')]
                       + ([(str(root / 'gachaops/assets/branding/program.png'), 'gachaops/assets/branding')]
                          if (root / 'gachaops/assets/branding/program.png').exists() else []),
    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=0,
)
a.binaries = [entry for entry in a.binaries
              if Path(entry[0]).name.lower() != 'icuuc.dll'
              and not (Path(entry[0]).name.lower().startswith('icudt')
                       and Path(entry[0]).suffix.lower() == '.dll')]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='GachaOps',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False, uac_admin=False,
          icon=str(root / 'gachaops/assets/branding/program.ico') if (root / 'gachaops/assets/branding/program.ico').exists() else None)
collect = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='GachaOps')
