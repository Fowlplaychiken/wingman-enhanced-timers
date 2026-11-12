"""Stub the Wingman modules the skill imports so tests need no Wingman install."""
import sys
from enum import Enum
from types import ModuleType, SimpleNamespace


def _mod(name, **attrs):
    m = ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class _Speech(Enum):
    EVENT = "event"


class _Skill:
    def __init__(self, config, settings, wingman):
        self.config, self.wingman, self.settings = config, wingman, settings
        self.log = SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None)

    async def prepare(self): ...
    async def unload(self): ...
    async def get_prompt(self): return None
    def get_generated_files_dir(self): raise AssertionError("tests must set storage_path")


_mod("api"); _mod("api.enums", SpeechPriority=_Speech)
_mod("skills"); _mod("skills.skill_base", Skill=_Skill, tool=lambda **k: (lambda f: f))
