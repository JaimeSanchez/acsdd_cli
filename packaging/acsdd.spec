# PyInstaller spec for the acsdd standalone binary.
#
# Bundles the JSON schemas explicitly (they're loaded at runtime via
# importlib.resources.files("acsdd.schemas"), which requires the data to
# be present in the frozen build's package tree at that exact path) plus
# jsonschema_specifications' own data as defense-in-depth, since jsonschema
# delegates its Draft 2020-12 meta-schemas to that package.
#
# Build with:
#   pyinstaller packaging/acsdd.spec --distpath dist --workpath build --clean

import os

from PyInstaller.utils.hooks import collect_data_files

repo_root = os.path.abspath(os.path.join(os.path.dirname(SPEC), ".."))

datas = [
    (
        os.path.join(repo_root, "src", "acsdd", "schemas", "capability.schema.json"),
        "acsdd/schemas",
    ),
    (
        os.path.join(repo_root, "src", "acsdd", "schemas", "profile.schema.json"),
        "acsdd/schemas",
    ),
    # Claude Code skills installed into a consumer repo by `acsdd skill
    # install`, read via importlib.resources.files("acsdd.assets").
    (
        os.path.join(repo_root, "src", "acsdd", "assets", "claude", "skills",
                     "profile-review", "SKILL.md"),
        "acsdd/assets/claude/skills/profile-review",
    ),
]
datas += collect_data_files("jsonschema_specifications")

a = Analysis(
    [os.path.join(repo_root, "packaging", "acsdd_entry.py")],
    pathex=[repo_root],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
# Passing a.binaries/a.zipfiles/a.datas directly into EXE (rather than via a
# separate COLLECT()) is what makes this a onefile build.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="acsdd",
    console=True,
    strip=False,
    upx=False,
)
