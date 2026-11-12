# Wingman Enhanced Timers

A Wingman AI custom skill that replaces the stock Timer with one that:

- **Remembers timers** across a Wingman restart (saved to `timers.json`, atomic writes).
- **Never fires missed timers silently.** Timers that came due while Wingman was closed are announced
  aloud, listed in status, and not run. Say "run missed timer" or cancel them.
- **Accepts clock times**: "remind me at 8 PM" (`at_time`: `20:00`, `8:00 PM` or ISO 8601, local time;
  already passed today means tomorrow; the resolved time is repeated back).
- **Asks before sending game input.** A due tool whose name contains a word from `confirm_before_tools`
  (default `key, press, hold, execute_keyboard, send_input`), or a stored request when
  `confirm_prompt_actions` is on, says "Timer X wants to ... Say confirm timer to do it." and waits.
  Nothing runs without the yes; after `confirm_window_seconds` (60) it is skipped and reported.

Three action kinds: reminder (`remind_me`, speaks a message), tool (any Wingman tool or command), and
prompt (`prompt` with `{"request": ...}`).

## Install (Wingman AI on Windows)

1. Close Wingman AI.
2. Copy the `enhanced_timers` folder into `%APPDATA%\ShipBit\WingmanAI\custom_skills\`.
3. Start Wingman, open your wingman's skills, **disable the stock Timer** and enable **Enhanced Timers**.
   Do not enable both: they share tool names (`set_timer`, `cancel_timer`, ...) and would disagree.

## Tools

`set_timer` (function_name, function_arguments_json, delay or at_time, is_loop, loop_counts, silent),
`get_timer_status`, `cancel_timer`, `change_timer_settings`, `remind_me`, `run_missed_timer`, `confirm_timer`.
Argument names match the stock skill; `delay` is now optional and `at_time` added.

## Safety rule

No key press or stored request runs from a timer without an explicit "confirm timer". "Run missed timer"
counts as the player's explicit request for that one timer.

## Develop / test

```
pip install pytest ruff
python -m pytest -q
ruff check .
```
Tests stub Wingman's `api.*` and `skills.*` modules, so no Wingman install is needed.

## Known gaps

- **Prompt timers speak instead of running a full assistant turn.** Wingman has `Wingman.process(transcript=...)`
  (`wingmen/wingman.py`, upstream main) but the skill facade (`wingmen/facade.py`) does not expose it. The
  skill uses it automatically if a future version exposes it as `self.wingman.process`; the gap is recorded in `skill.gaps`.
- Untested inside a real Wingman install (only against stubs). The `@tool` schema generation, `prepare()`
  worker thread and `get_generated_files_dir()` path are copied from upstream usage, not run.
- Loop timers keep drifting from their original time by execution latency (same as stock).
- A timer's repeat after a clock-time `at_time` loop repeats every 24 h unless `delay` is also given.

## Starbuck refine next

1. Install on Nick's Wingman, check `@tool` schema accepts `Optional` args and `function_arguments_json` default.
2. Confirm the stock Timer can be disabled cleanly and the generated-files path persists across updates.
3. Decide whether the stock "summary" speech via `ai.generate` is wanted for tool timers.
4. Replace the prompt-action fallback once a full-turn API is available.
5. Add an in-game test of the confirm flow with a real key tool, with the player present.
