import hashlib
import json
import zipfile

from build import build


def test_package_contains_only_skill_and_docs_with_matching_manifest(tmp_path):
    result = build(tmp_path / "package")
    with zipfile.ZipFile(result["archive"]) as archive:
        names = set(archive.namelist())
        expected = {"main.py", "__init__.py", "default_config.yaml", "README.md", "LICENSE"}
        assert names == {"enhanced_timers/" + name for name in expected | {"manifest.json"}}
        manifest = json.loads(archive.read("enhanced_timers/manifest.json"))
        assert set(manifest["files"]) == expected
        for name, digest in manifest["files"].items():
            assert hashlib.sha256(archive.read("enhanced_timers/" + name)).hexdigest() == digest
