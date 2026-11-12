# Wingman Enhanced Timers

Voice alarms, reminders and scheduled assistant requests for Wingman AI. Timers survive a Wingman restart. No Wingman Core changes or separate service.

## What you can say

- “In ten minutes, remind me to check the refinery.”
- “At 8 PM, remind me to leave.”
- “Every fifteen minutes, remind me to check my fuel.”
- “In five minutes, check my quantum fuel and tell me.”
- “List my timers.” / “Cancel the refinery timer.”

A reminder speaks directly. A scheduled assistant request asks for confirmation when due, then submits the request to your usual Wingman. That assistant uses its normal model, tools and action controls. Submission does not mean its task succeeded.

Timers missed while Wingman was closed are announced and listed; they never run silently on startup. Ask to run or cancel one.

## Install

Requires Wingman AI 3.2.14 or later and its public v3 skills API. Wingman already supplies the `httpx` dependency used for its local API.

1. Download the skill ZIP from [Releases](https://github.com/Fowlplaychiken/wingman-enhanced-timers/releases).
2. Fully quit Wingman. Extract the `enhanced_timers` folder into `%APPDATA%\ShipBit\WingmanAI\custom_skills`.
3. Reopen Wingman. For your chosen assistant, disable the stock **Timer** and enable **Enhanced Timers**. Use only one: they share timer tool names.
4. Try a short reminder, then ask to list your timers.

Existing stock timers cannot be imported because stock Wingman holds them only in memory. Finish or cancel those before switching. Keep the prior skill version for rollback.

## When a timer acts

Reminders run when due. Commands that contain input, unfamiliar action tools and stored requests ask you to say **“confirm timer”** first. If several await confirmation, name the timer. The confirmation window defaults to 60 seconds; queued speech can reduce the time available, so ask for timer status if unsure. Unconfirmed timers are skipped and reported.

A cancelled timer cannot be started from a stale worker snapshot. An action that has already begun may have an uncertain outcome; the skill does not automatically repeat it. A scheduled tool cannot confirm another timer or run a missed timer on your behalf.

Turn on **Dry run actions** to check a timer before letting it act. Set a short action or request timer, confirm it when asked, then say “show my dry-run log” to see what would have happened. Stored requests and actions requiring confirmation are recorded instead of run. Reminders and reviewed read-only tools still run.

```mermaid
flowchart TD
    Due[Timer becomes due] --> Kind{Reminder?}
    Kind -- Yes --> Speak[Speak reminder]
    Kind -- Action or request --> Need{Confirmation needed?}
    Need -- Yes --> Ask[Ask you to confirm]
    Ask -- Confirmed --> Dry{Dry run applies?}
    Ask -- Not confirmed --> Skip[Skip and report]
    Need -- No --> Dry
    Dry -- Yes --> Record[Record what would happen]
    Dry -- No --> Run[Run action or submit request]
    Missed[Timer missed while closed] --> Hold[Announce and keep on hold]
    Hold -- You ask to run it --> Dry
    Hold -- You cancel it --> Remove[Remove timer]
```

## Settings

| Setting | Default | Use |
|---|---|---|
| Timer file | automatic | Private JSON file scoped to your assistant name; optional explicit path. Keep private: it contains reminder/request text. |
| Tools allowed without confirmation | `get_weather` | Exact reviewed tool names. Unknown tools ask first. Input commands still ask. |
| Ask before stored requests | on | Confirmation before submitting a full assistant turn. |
| Core API port | `49111` | Change only if your Wingman uses another local port. Fixed loopback only; no external server. |
| Confirm window | 60 seconds | Time to confirm a due action. |
| Check interval | 2 seconds | How often timers are checked. |
| Dry run actions | off | Record stored requests and actions requiring confirmation instead of running them. |

Different assistant names get separate saved files. If you use the same assistant name in multiple profiles, set a distinct **Timer file** for each; the public skill API does not expose profile identity. The old unscoped `timers.json` is kept untouched; choose it explicitly only if you intend to reuse it.

Clock times use your PC's local timezone. “Noon” and “midnight” work. A clock time already passed today means tomorrow; an explicit past date is refused. The resolved time is repeated back. Repeating clock-time alarms use 24 hours; change_timer_settings can change that interval. They may drift with execution latency; daylight-saving/calendar recurrence is not implemented.

## Diagnostics and limits

Ask to list timers to see pending, missed and awaiting-confirmation states. Errors go to Wingman's skill log. Corrupt or unsupported timer data is retained as a `.bad` file and reported; no actions are inferred from it. Scheduled requests use the original local Core API, check the active profile, and are submitted once. A timeout after submission is reported as unknown and is not retried.

Offline tests cover persistence, cancellation, confirmation, dry runs, worker shutdown and local API submission. Native installation/restart and actual assistant completion are separate checks; source tests do not prove game behavior.

## Development

Python 3.11 or later. Install test dependencies in your development environment, then run:

```sh
python -m pytest -q
ruff check .
python build.py --out /path/to/new/output-folder
```

The deterministic ZIP contains only this skill, its README/license and a hash manifest. It contains no user timers, credentials, recordings, models or other project code. Tests mock Wingman and HTTP calls; they do not contact a provider.

Based on the stock [Wingman Timer](https://github.com/ShipBit/wingman-ai/tree/main/skills/timer), under MIT. This optional module is independently maintained and does not modify Wingman Core.
