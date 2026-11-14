import hashlib
import io
import importlib.util
import json
import sys
import wave
import zipfile

from build import build
from enhanced_timers.alarm_sounds import SOUNDS


def test_package_contains_only_skill_and_docs_with_matching_manifest(tmp_path, monkeypatch):
    result = build(tmp_path / "package")
    with zipfile.ZipFile(result["archive"]) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("enhanced_timers/manifest.json"))
        module = "enhanced_timers_v" + manifest["version"].replace(".", "_")
        dependency = f"dependencies/{module}"
        expected = {"main.py", "__init__.py", "default_config.yaml", "README.md", "LICENSE"}
        expected |= {f"{dependency}/{name}" for name in ("__init__.py", "timezones.whl", "alarm_sounds.py", "main.py")}
        expected |= {f"{dependency}/sounds/{name}.wav" for name in SOUNDS}
        assert names == {"enhanced_timers/" + name for name in expected | {"manifest.json"}}
        assert set(manifest["files"]) == expected
        for name, digest in manifest["files"].items():
            assert hashlib.sha256(archive.read("enhanced_timers/" + name)).hexdigest() == digest
        for name, sound in SOUNDS.items():
            with wave.open(io.BytesIO(archive.read(f"enhanced_timers/{dependency}/sounds/{name}.wav"))) as clip:
                assert clip.getnchannels() == 1 and clip.getsampwidth() == 2
                assert abs(clip.getnframes() / clip.getframerate() - sound.duration) < 0.001
        archive.extractall(tmp_path / "unpacked")
    root = tmp_path / "unpacked" / "enhanced_timers"
    monkeypatch.syspath_prepend(str(root / "dependencies"))
    spec = importlib.util.spec_from_file_location("main", root / "main.py")
    entry = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(entry)
        assert entry.EnhancedTimers.__module__ == f"{module}.main"
    finally:
        for name in list(sys.modules):
            if name == module or name.startswith(module + "."):
                sys.modules.pop(name)
