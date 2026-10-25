"""Write build/version_info.txt (Windows version resource for PyInstaller) from the VERSION file."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
parts = tuple(int(p) for p in (version.split(".") + ["0", "0", "0"])[:4])

info = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={parts}, prodvers={parts}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Lucca Gugescu'),
      StringStruct('FileDescription', 'pgstress - PostgreSQL seed, profile and stress test'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'pgstress'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 Lucca Gugescu. MIT License.'),
      StringStruct('OriginalFilename', 'pgstress.exe'),
      StringStruct('ProductName', 'pgstress'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
out = ROOT / "build" / "version_info.txt"
out.parent.mkdir(exist_ok=True)
out.write_text(info, encoding="utf-8")
print(version)
