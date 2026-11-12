import asyncio
import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from enhanced_timers.main import EnhancedTimers, parse_at_time


@pytest.fixture(autouse=True)
def core_api(monkeypatch):
    """Every test uses loopback transport in memory; never contact a real Core."""
    state = {"calls": [], "timeout": False, "missing": False}
    original = httpx.AsyncClient
    def handle(request):
        state["calls"].append(request)
        if request.url.path == "/configs":
            return httpx.Response(200, json={"current_config_dir": {"name": "Test Profile"}})
        if request.url.path == "/config":
            return httpx.Response(200, json={"config": {"wingmen": {} if state["missing"] else {"Starbuck": {}}}})
        assert request.url.path == "/send-text-to-wingman"
        if state["timeout"]:
            raise httpx.ReadTimeout("test response lost", request=request)
        return httpx.Response(202)
    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)
    monkeypatch.setattr("enhanced_timers.main.httpx.AsyncClient", client)
    return state


class Fake:
    def __init__(self):
        self.name = "Starbuck"
        self.spoken, self.invoked, self.processed = [], [], []
        self.tools = SimpleNamespace(has=lambda n: True, invoke=self._invoke)
        self.commands = SimpleNamespace(get=lambda n: None)
        self.tts = SimpleNamespace(speak=self._speak)
        self.conversation = SimpleNamespace(add_assistant=self._noop, user_busy=False)
        self.ai = SimpleNamespace(generate=self._noop)
        self.language = SimpleNamespace(name="English")

    async def _noop(self, *a, **k): return None
    async def _speak(self, text, priority=None): self.spoken.append(text)

    async def _invoke(self, name, args):
        self.invoked.append((name, args))
        return SimpleNamespace(instant_response=None, response="done")

    def run_in_thread(self, fn, *a): return None


def make(tmp_path, clock, wingman=None, **props):
    p = {"storage_path": str(tmp_path / "t.json"), "confirm_window_seconds": 60, **props}
    cfg = SimpleNamespace(custom_properties=[SimpleNamespace(id=k, value=v) for k, v in p.items()])
    s = EnhancedTimers(cfg, None, wingman or Fake())
    s.clock = lambda: clock[0]
    return s


def run(c): return asyncio.run(c)


T0 = 1_800_000_000.0


def test_one_shot_fires_once(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("remind_me", '{"message": "tea"}', delay=10)
        await s.tick(); assert s.wingman.spoken == []
        c[0] += 10; await s.tick(); await s.tick()
        c[0] += 100; await s.tick()
        assert s.wingman.spoken == ["tea"] and s.timers == {}
    run(go())


def test_three_repeats_fire_exactly_three_times(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("remind_me", '{"message": "x"}', delay=5, is_loop=True, loop_counts=3)
        for _ in range(10):
            c[0] += 5; await s.tick()
        assert s.wingman.spoken == ["x"] * 3 and s.timers == {}
    run(go())


def test_cancel_stops_pending(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("remind_me", '{"message": "x"}', delay=5)
        await s.cancel_timer(next(iter(s.timers)))
        c[0] += 50; await s.tick()
        assert s.wingman.spoken == []
    run(go())


def test_restart_resumes_future_timer(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("remind_me", '{"message": "later"}', delay=60)
        c[0] += 10
        s2 = make(tmp_path, c)
        await s2.load_and_announce()
        assert s2.wingman.spoken == []          # nothing missed
        c[0] += 50; await s2.tick()
        assert s2.wingman.spoken == ["later"]
    run(go())


def test_missed_timer_announced_not_run_until_asked(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("some_tool", "{}", delay=30)
        c[0] += 500
        s2 = make(tmp_path, c)
        await s2.load_and_announce(); await s2.tick()
        assert s2.wingman.invoked == []
        assert len(s2.wingman.spoken) == 1 and "did not run" in s2.wingman.spoken[0]
        assert (await s2.get_timer_status())[0]["state"] == "missed"
        # a second restart must not re-announce or run it
        s3 = make(tmp_path, c); await s3.load_and_announce(); await s3.tick()
        assert s3.wingman.spoken == [] and s3.wingman.invoked == []
        await s3.run_missed_timer(next(iter(s3.timers)))
        assert s3.wingman.invoked == [("some_tool", {})] and s3.timers == {}
    run(go())


def test_key_tool_asks_first_then_runs_once_on_confirm(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("press_key", '{"key": "g"}', delay=5)
        c[0] += 5; await s.tick(); await s.tick()
        assert s.wingman.invoked == []
        assert "Say confirm timer" in s.wingman.spoken[-1]
        assert "press_key" in s.wingman.spoken[-1]
        c[0] += 10; await s.tick()
        assert s.wingman.invoked == []          # still nothing without the yes
        await s.confirm_timer()
        await s.confirm_timer()
        assert s.wingman.invoked == [("press_key", {"key": "g"})]
        assert s.timers == {}
    run(go())


def test_unconfirmed_expires_and_is_skipped(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("hold_trigger", "{}", delay=5)
        c[0] += 5; await s.tick()
        c[0] += 61; await s.tick()
        assert s.wingman.invoked == [] and s.timers == {}
        assert "skipped" in s.wingman.spoken[-1]
        assert "No timer" in await s.confirm_timer()
    run(go())


def test_late_confirm_is_refused(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("press_key", "{}", delay=5)
        c[0] += 5; await s.tick()
        c[0] += 61
        assert "skipped" in await s.confirm_timer()
        assert s.wingman.invoked == []
    run(go())


def test_non_input_tool_runs_without_asking(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("get_weather", "{}", delay=5, silent=True)
        c[0] += 5; await s.tick()
        assert s.wingman.invoked == [("get_weather", {})]
    run(go())


def test_prompt_asks_first_then_submits_one_full_turn(tmp_path, core_api):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("prompt", '{"request": "check fuel"}', delay=5)
        c[0] += 5; await s.tick()
        assert "confirm timer" in s.wingman.spoken[-1]
        assert core_api["calls"] == []
        await s.confirm_timer()
        assert core_api["calls"] == []
        await s.tick()
        assert s.wingman.spoken[-1].startswith("Stored request submitted")
        posts = [r for r in core_api["calls"] if r.method == "POST"]
        assert len(posts) == 1
        assert posts[0].content == b"text=check+fuel&wingman_name=Starbuck"
        assert str(posts[0].url) == "http://127.0.0.1:49111/send-text-to-wingman"
    run(go())


def test_at_time_past_today_schedules_tomorrow(tmp_path):
    async def go():
        now = datetime.fromtimestamp(T0).astimezone()
        past = (now - timedelta(hours=1)).strftime("%H:%M")
        c = [T0]; s = make(tmp_path, c)
        r = await s.set_timer("remind_me", '{"message": "x"}', at_time=past)
        due = next(iter(s.timers.values()))["due"]
        assert 23 * 3600 <= due - T0 <= 24 * 3600
        assert "tomorrow" in r
        future = (now + timedelta(hours=2)).replace(second=0, microsecond=0)
        assert parse_at_time(future.strftime("%I:%M %p"), now) == future
    run(go())


def test_needs_exactly_one_of_delay_or_at_time(tmp_path):
    async def go():
        s = make(tmp_path, [T0])
        assert (await s.set_timer("remind_me", "{}")).startswith("Error")
        assert (await s.set_timer("remind_me", "{}", delay=5, at_time="20:00")).startswith("Error")
        assert s.timers == {}
    run(go())


def test_corrupt_file_kept_as_bad_and_start_continues(tmp_path):
    async def go():
        (tmp_path / "t.json").write_text("{not json")
        s = make(tmp_path, [T0])
        await s.load_and_announce()
        assert (tmp_path / "t.json.bad").read_text() == "{not json"
        assert "could not be read" in s.wingman.spoken[0]
        await s.set_timer("remind_me", '{"message": "ok"}', delay=5)
        assert len(s.timers) == 1 and os.path.exists(tmp_path / "t.json")
    run(go())


def test_identical_timer_refused_but_different_one_allowed(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("remind_me", '{"message": "tea"}', delay=300)
        dup = await s.set_timer("remind_me", '{"message": "tea"}', delay=300)
        other = await s.set_timer("remind_me", '{"message": "bread"}', delay=300)
        assert dup.startswith("Error: an identical timer") and len(s.timers) == 2
        assert other.startswith("Timer set")
    run(go())


def test_noon_and_midnight_words():
    now = datetime(2026, 11, 12, 13, 0).astimezone()
    assert parse_at_time("noon", now).hour == 12 and parse_at_time("noon", now).day == 13
    assert parse_at_time("Midnight", now).hour == 0 and parse_at_time("midnight", now).day == 13



def test_dry_run_logs_key_press_and_never_executes(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c, dry_run_key_presses=True)
        await s.set_timer("press_key", '{"key": "g"}', delay=5)
        c[0] += 5; await s.tick()
        assert s.wingman.invoked == []
        c[0] += 2; await s.confirm_timer()
        assert s.wingman.invoked == []
        log = await s.get_dry_run_log()
        assert len(log) == 1
        e = log[0]
        assert (e["tool"], e["arguments"], e["due"], e["actual"], e["reason"]) == (
            "press_key", {"key": "g"}, T0 + 5, T0 + 7, "dry_run_not_sent")
        assert "Dry run: would have pressed" in s.wingman.spoken[-1]
        assert s.timers == {}                      # one-shot is done
    run(go())


def test_dry_run_loop_still_rearms(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c, dry_run_key_presses=True)
        await s.set_timer("press_key", "{}", delay=5, is_loop=True, loop_counts=2)
        c[0] += 5; await s.tick(); await s.confirm_timer()
        t = next(iter(s.timers.values()))
        assert t["state"] == "pending" and t["loops"] == 1
        assert s.wingman.invoked == [] and len(s.dry_run_log) == 1
    run(go())


def test_dry_run_off_executes_after_confirm(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c, dry_run_key_presses=False)
        await s.set_timer("press_key", '{"key": "g"}', delay=5)
        c[0] += 5; await s.tick()
        assert s.wingman.invoked == []
        await s.confirm_timer()
        assert s.wingman.invoked == [("press_key", {"key": "g"})]
        assert await s.get_dry_run_log() == []
    run(go())


def test_dry_run_does_not_dispatch_prompt_timers(tmp_path, core_api):
    async def go():
        c = [T0]; s = make(tmp_path, c, dry_run_key_presses=True)
        await s.set_timer("prompt", '{"request": "check fuel"}', delay=5)
        c[0] += 5; await s.tick(); await s.confirm_timer()
        assert s.wingman.spoken[-1] == "Dry run: would have run stored request check fuel"
        assert s.dry_run_log[0]["tool"] == "prompt"
        assert core_api["calls"] == []
    run(go())


def test_dry_run_leaves_non_key_tools_running(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c, dry_run_key_presses=True)
        await s.set_timer("get_weather", "{}", delay=5, silent=True)
        c[0] += 5; await s.tick()
        assert s.wingman.invoked == [("get_weather", {})] and s.dry_run_log == []
    run(go())


def test_benign_named_keyboard_command_confirms_and_dry_runs(tmp_path):
    async def go():
        c = [T0]; w = Fake()
        command = SimpleNamespace(actions=[SimpleNamespace(keyboard=object())])
        w.commands.get = lambda name: command if name == "Landing Gear" else None
        w.tools.has = lambda name: name != "Landing Gear"
        s = make(tmp_path, c, w, dry_run_key_presses=True)
        await s.set_timer("Landing Gear", delay=5)
        c[0] += 5; await s.tick()
        assert w.invoked == []
        assert next(iter(s.timers.values()))["state"] == "awaiting"
        await s.confirm_timer()
        assert w.invoked == []
        assert (await s.get_dry_run_log())[0]["tool"] == "execute_command"
    run(go())


def test_cancel_due_timer_while_another_speaks(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        entered, release = asyncio.Event(), asyncio.Event()
        async def speak(text, priority=None):
            if text == "first":
                entered.set(); await release.wait()
            s.wingman.spoken.append(text)
        s.wingman.tts.speak = speak
        await s.set_timer("remind_me", '{"message":"first"}', delay=5)
        await s.set_timer("get_weather", delay=5)
        second = next(t["id"] for t in s.timers.values() if t["action"]["name"] == "get_weather")
        c[0] += 5
        ticking = asyncio.create_task(s.tick())
        await entered.wait(); await s.cancel_timer(second); release.set(); await ticking
        assert s.wingman.invoked == []
    run(go())


def test_cannot_schedule_confirmation_or_missed_execution(tmp_path):
    async def go():
        s = make(tmp_path, [T0])
        for name in ("confirm_timer", "run_missed_timer"):
            assert (await s.set_timer(name, delay=5)).startswith("Error")
        assert s.timers == {}
    run(go())


def test_invalid_saved_timer_is_quarantined(tmp_path):
    async def go():
        path = tmp_path / "t.json"
        path.write_text(json.dumps({"version": 1, "timers": [{"id": "bad", "due": T0 + 50}]}))
        s = make(tmp_path, [T0]); await s.load_and_announce()
        assert s.timers == {}
        assert (tmp_path / "t.json.bad").exists()
    run(go())


class ThreadedFake(Fake):
    def run_in_thread(self, fn, *args):
        def work():
            asyncio.run(fn(*args))
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        return worker


def test_worker_survives_caller_loop_and_unload_awaits_owned_thread(tmp_path):
    s = make(tmp_path, [T0], ThreadedFake(), poll_seconds=0.05)
    run(s.prepare())  # the assistant turn's loop now closes
    assert s._thread.is_alive()
    run(s.unload())
    assert not s._thread.is_alive()
    assert s._worker_loop is None and s._worker_task is None


def test_unload_cancels_inflight_worker_speech_without_replaying(tmp_path):
    async def go():
        c = [T0]; w = ThreadedFake(); entered = threading.Event()
        async def speak(text, priority=None):
            entered.set()
            await asyncio.sleep(60)
        w.tts.speak = speak
        s = make(tmp_path, c, w, poll_seconds=0.05)
        await s.prepare()
        await s.set_timer("remind_me", '{"message":"first"}', delay=5)
        c[0] += 5
        try:
            assert await asyncio.to_thread(entered.wait, 2)
        finally:
            await s.unload()
        assert not s._thread.is_alive()
        restored = make(tmp_path, c)
        await restored.load_and_announce()
        assert restored.timers == {}  # consumed before the uncertain external effect
    run(go())


def test_unknown_tool_requires_confirmation_but_reviewed_read_tool_does_not(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("engage", delay=5)
        await s.set_timer("get_weather", delay=5)
        c[0] += 5; await s.tick()
        assert s.wingman.invoked == [("get_weather", {})]
        assert next(iter(s.timers.values()))["state"] == "awaiting"
    run(go())


def test_bad_saved_version_is_quarantined(tmp_path):
    async def go():
        (tmp_path / "t.json").write_text(json.dumps({"version": 999, "timers": []}))
        s = make(tmp_path, [T0]); await s.load_and_announce()
        assert (tmp_path / "t.json.bad").exists()
    run(go())


def test_prompt_timeout_is_unknown_and_never_retried(tmp_path, core_api):
    async def go():
        core_api["timeout"] = True
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("prompt", '{"request":"check fuel"}', delay=5)
        c[0] += 5; await s.tick(); await s.confirm_timer(); await s.tick()
        assert "outcome is unknown" in s.wingman.spoken[-1]
        c[0] += 100; await s.tick(); await s.confirm_timer()
        assert len([r for r in core_api["calls"] if r.method == "POST"]) == 1
        assert s.timers == {}
    run(go())


def test_prompt_wrong_profile_and_invalid_port_never_post(tmp_path, core_api):
    async def go():
        core_api["missing"] = True
        s = make(tmp_path, [T0])
        await s._run_prompt("check fuel", False)
        assert "was not submitted" in s.wingman.spoken[-1]
        assert not any(r.method == "POST" for r in core_api["calls"])
        core_api["calls"].clear()
        s = make(tmp_path, [T0], core_api_port="http://elsewhere")
        await s._run_prompt("check fuel", False)
        assert core_api["calls"] == []
    run(go())


def test_empty_safe_tool_list_requires_confirmation(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c, unconfirmed_tools="")
        await s.set_timer("get_weather", delay=5)
        c[0] += 5; await s.tick()
        assert s.wingman.invoked == []
    run(go())


def test_default_storage_isolated_by_wingman_name_and_preserves_legacy(tmp_path):
    async def go():
        legacy = tmp_path / "timers.json"
        legacy.write_text('{"legacy":"preserve"}')
        first = make(tmp_path, [T0], storage_path="")
        second_wingman = Fake(); second_wingman.name = "Other/Ship"
        second = make(tmp_path, [T0], second_wingman, storage_path="")
        for s in (first, second):
            s.get_generated_files_dir = lambda: str(tmp_path)
        assert first._path() != second._path()
        assert os.path.dirname(second._path()) == str(tmp_path)
        await first.set_timer("remind_me", '{"message":"first"}', delay=5)
        await second.load_and_announce()
        assert second.timers == {}
        await second.set_timer("remind_me", '{"message":"second"}', delay=10)
        with open(first._path(), encoding="utf-8") as saved:
            assert json.load(saved)["timers"][0]["action"]["args"] == {"message": "first"}
        assert legacy.read_text() == '{"legacy":"preserve"}'
    run(go())


def test_enabled_skill_starts_saved_timer_at_startup_without_tool_invocation(tmp_path):
    async def go():
        defaults = yaml.safe_load((Path(__file__).parents[1] / "enhanced_timers/default_config.yaml").read_text())
        c = [T0]; seed = make(tmp_path, c)
        await seed.set_timer("remind_me", '{"message":"restored"}', delay=5)
        w = ThreadedFake(); spoken = threading.Event()
        async def speak(text, priority=None):
            w.spoken.append(text); spoken.set()
        w.tts.speak = speak
        restored = make(tmp_path, c, w, poll_seconds=0.05)
        try:
            # WingmanSkillManager.prepare_skill calls ensure_activated only for this flag.
            if defaults["auto_activate"]:
                await restored.prepare()
            assert restored.active
            c[0] += 5
            assert await asyncio.to_thread(spoken.wait, 2)
            assert w.spoken == ["restored"]
        finally:
            await restored.unload()
    run(go())


def test_user_triggered_timer_speech_uses_answer_default_and_worker_uses_event(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c, dry_run_key_presses=True)
        priorities = []
        async def speak(text, priority=None):
            priorities.append(priority)
        s.wingman.tts.speak = speak
        await s.set_timer("press_key", delay=5)
        c[0] += 5; await s.tick()
        assert priorities[-1] is not None  # autonomous confirmation announcement is EVENT
        await s.confirm_timer()
        assert priorities[-1] is None  # facade's default is ANSWER, never EVENT inside this turn
        await s.set_timer("remind_me", '{"message":"missed"}', delay=5)
        c[0] += 10
        await s.load_and_announce()
        assert priorities[-1] is not None
        await s.run_missed_timer(next(iter(s.timers)))
        assert priorities[-1] is None
        await s.set_timer("remind_me", '{"message":"autonomous"}', delay=5)
        c[0] += 5; await s.tick()
        assert priorities[-1] is not None  # request context reset after both endpoints
    run(go())


def test_confirmed_prompt_waits_for_confirmation_turn_to_finish(tmp_path, core_api):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("prompt", '{"request":"check fuel"}', delay=5)
        c[0] += 5; await s.tick()
        s.wingman.conversation.user_busy = True
        assert "queued" in await s.confirm_timer()
        assert next(iter(s.timers.values()))["state"] == "confirmed"
        await s.tick()
        assert core_api["calls"] == []
        s.wingman.conversation.user_busy = False
        await s.tick(); await s.tick()
        assert len([r for r in core_api["calls"] if r.method == "POST"]) == 1
    run(go())


def test_cancelled_queued_prompt_never_submits(tmp_path, core_api):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("prompt", '{"request":"check fuel"}', delay=5)
        c[0] += 5; await s.tick()
        s.wingman.conversation.user_busy = True
        await s.confirm_timer()
        await s.cancel_timer(next(iter(s.timers)))
        s.wingman.conversation.user_busy = False
        await s.tick()
        assert core_api["calls"] == []
    run(go())


def test_restart_holds_confirmed_prompt_as_missed(tmp_path, core_api):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("prompt", '{"request":"check fuel"}', delay=5)
        c[0] += 5; await s.tick()
        s.wingman.conversation.user_busy = True
        await s.confirm_timer()
        restored = make(tmp_path, c)
        await restored.load_and_announce(); await restored.tick()
        assert next(iter(restored.timers.values()))["state"] == "missed"
        assert core_api["calls"] == []
    run(go())


def test_queued_prompt_expires_without_submitting(tmp_path, core_api):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("prompt", '{"request":"check fuel"}', delay=5)
        c[0] += 5; await s.tick()
        s.wingman.conversation.user_busy = True
        await s.confirm_timer()
        c[0] += 61; await s.tick()
        s.wingman.conversation.user_busy = False
        await s.tick()
        assert s.timers == {} and core_api["calls"] == []
    run(go())


def test_running_missed_prompt_also_waits_for_user_turn(tmp_path, core_api):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("prompt", '{"request":"check fuel"}', delay=5)
        c[0] += 10; await s.load_and_announce()
        s.wingman.conversation.user_busy = True
        assert "queued" in await s.run_missed_timer(next(iter(s.timers)))
        await s.tick()
        assert core_api["calls"] == []
        s.wingman.conversation.user_busy = False
        await s.tick()
        assert len([r for r in core_api["calls"] if r.method == "POST"]) == 1
    run(go())
