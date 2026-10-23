"""Build a reproducible private-test ZIP using an explicit file allowlist."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

ROOT_FILES = (
    "__init__.py",
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "pyproject.toml",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
)


def build(root: Path, output: Path) -> Path:
    version = re.search(
        r"^version:\s*v?([^\s]+)", (root / "metadata.yaml").read_text(), re.MULTILINE
    ).group(1)
    files = [root / name for name in ROOT_FILES] + [root / "configs/minimal.json"]
    example = json.loads((root / "configs/minimal.json").read_text())
    schema = json.loads((root / "_conf_schema.json").read_text())
    if any(
        example.get(key) or schema[key].get("default")
        for key in ("alpaca_key_id", "alpaca_secret_key")
    ):
        raise ValueError("Release example/schema must not contain credentials")
    for directory, pattern in (("stockwatcher", "*.py"), ("docs", "*.md")):
        files.extend(sorted((root / directory).rglob(pattern)))
    entries = {}
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Release file cannot be a symlink or outside the source tree")
        entries[path.relative_to(root).as_posix()] = path.read_bytes()
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, text=True, capture_output=True, check=True
        ).stdout.strip()
    )
    manifest = {
        "version": version,
        "source_commit": revision,
        "source_dirty": dirty,
        "files": {
            name: hashlib.sha256(content).hexdigest() for name, content in sorted(entries.items())
        },
    }
    entries["RELEASE_MANIFEST.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"astrbot_plugin_stockwatcher-v{version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, content in sorted(entries.items()):
            info = zipfile.ZipInfo(
                "astrbot_plugin_stockwatcher/" + name, date_time=(2026, 1, 1, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            package.writestr(info, content)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix + ".sha256").write_text(f"{checksum}  {archive.name}\n")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    print(build(Path(__file__).resolve().parents[1], args.output))
