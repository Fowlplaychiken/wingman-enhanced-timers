"""Package the optional skill without importing Wingman or personal data."""

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TZDATA_SHA256 = "0a10eec6e13ef16b627fabed9a5f2ba61a62b1c67f327305f4e7e412a70b4aec"


def build(destination: Path) -> dict:
    destination = destination.resolve()
    if destination == ROOT or ROOT in destination.parents:
        raise ValueError("Use a new output folder outside the source checkout")
    source = ROOT / "enhanced_timers"
    config = (source / "default_config.yaml").read_bytes()
    version = re.search(rb"(?m)^version: ([0-9]+\.[0-9]+\.[0-9]+)\r?$", config)
    if version is None:
        raise ValueError("Explicit skill version required")
    timezones = (source / "timezones.whl").read_bytes()
    if hashlib.sha256(timezones).hexdigest() != TZDATA_SHA256:
        raise ValueError("Bundled tzdata differs from the pinned upstream wheel")
    members = {
        "default_config.yaml": config,
        "main.py": (source / "main.py").read_bytes(),
        "__init__.py": (source / "__init__.py").read_bytes(),
        "README.md": (ROOT / "README.md").read_bytes(),
        "LICENSE": (ROOT / "LICENSE").read_bytes(),
        "timezones.whl": timezones,
    }
    manifest = {
        "version": version.group(1).decode("ascii"),
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in members.items()},
    }
    members["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    destination.mkdir(parents=True, exist_ok=False)
    archive = destination / f"enhanced_timers-{manifest['version']}.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in sorted(members.items()):
            entry = zipfile.ZipInfo("enhanced_timers/" + name, (1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(entry, data)
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        assert all(bundle.read("enhanced_timers/" + name) == data for name, data in members.items())
    return {"archive": str(archive), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(json.dumps(build(parser.parse_args().out)))
