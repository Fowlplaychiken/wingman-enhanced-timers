import asyncio
import os
from datetime import datetime, timedelta
from types import SimpleNamespace


from enhanced_timers.main import EnhancedTimers, parse_at_time


class Fake:
    def __init__(self):
        self.spoken, self.invoked, self.processed = [], [], []
        self.tools = SimpleNamespace(has=lambda n: True, invoke=self._invoke)
        self.commands = SimpleNamespace(get=lambda n: None)
        self.tts = SimpleNamespace(speak=self._speak)
        self.conversation = SimpleNamespace(add_assistant=self._noop)
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


def test_prompt_asks_first_and_degrades_to_speech(tmp_path):
    async def go():
        c = [T0]; s = make(tmp_path, c)
        await s.set_timer("prompt", '{"request": "check fuel"}', delay=5)
        c[0] += 5; await s.tick()
        assert "confirm timer" in s.wingman.spoken[-1]
        await s.confirm_timer()
        assert s.wingman.spoken[-1] == "Timer reminder: check fuel"
        assert s.gaps
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
