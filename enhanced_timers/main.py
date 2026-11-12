"""Enhanced Timers: persistent, clock-time, confirm-before-input timers.

Tool names and semantics follow ShipBit's MIT Timer skill (set_timer,
get_timer_status, cancel_timer, change_timer_settings, remind_me).
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import string
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from api.enums import SpeechPriority
from skills.skill_base import Skill, tool

if TYPE_CHECKING:
    from api.interface import SettingsConfig, SkillConfig
    from wingmen.wingman_context import WingmanContext

REMIND = "remind_me"
PROMPT = "prompt"
FILE_VERSION = 1


def parse_at_time(text: str, now: datetime) -> datetime:
    """Resolve '20:00', '8:00 PM', '8 PM' or ISO 8601 to the next such local time."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty time")
    tz = now.tzinfo
    raw = {"NOON": "12:00", "MIDNIGHT": "00:00"}.get(raw.upper(), raw)
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            t = datetime.strptime(raw.upper().replace(".", ""), fmt).time()
        except ValueError:
            continue
        due = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
        return due if due > now else due + timedelta(days=1)
    try:
        due = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Could not understand time '{text}'") from None
    if due.tzinfo is None:
        due = due.replace(tzinfo=tz)
    if due <= now:
        raise ValueError("That date and time is already in the past.")
    return due


class EnhancedTimers(Skill):
    def __init__(self, config: "SkillConfig", settings: "SettingsConfig",
                 wingman: "WingmanContext") -> None:
        super().__init__(config=config, settings=settings, wingman=wingman)
        self.timers: dict[str, dict] = {}
        self.clock = time.time
        self.active = False
        self.gaps: list[str] = []
        self._announced_load = False

    # ------------------------------------------------------------ config
    def _prop(self, prop_id: str, default):
        for p in getattr(self.config, "custom_properties", None) or []:
            if p.id == prop_id and p.value not in (None, ""):
                return p.value
        return default

    def _confirm_words(self) -> list[str]:
        raw = str(self._prop("confirm_before_tools",
                             "key,press,hold,execute_keyboard,send_input"))
        return [w.strip().lower() for w in raw.split(",") if w.strip()]

    def _path(self) -> str:
        custom = self._prop("storage_path", "")
        if custom:
            return str(custom)
        return os.path.join(self.get_generated_files_dir(), "timers.json")

    # ------------------------------------------------------- lifecycle
    async def prepare(self) -> None:
        await super().prepare()
        await self.load_and_announce()
        self.active = True
        self.wingman.run_in_thread(self.start_timer_worker)

    async def unload(self) -> None:
        await super().unload()
        self.active = False

    async def get_prompt(self) -> str | None:
        prompt = await super().get_prompt()
        return f"{prompt or ''}\n\nActive timers:\n{await self.get_timer_status()}"

    async def start_timer_worker(self) -> None:
        while self.active:
            await asyncio.sleep(float(self._prop("poll_seconds", 2)))
            if not self.active:
                break
            try:
                await self.tick()
            except Exception as e:  # keep the worker alive
                self.log.error(f"Timer tick failed: {e}", server_only=True)

    # ------------------------------------------------------ persistence
    def _save(self) -> None:
        path = self._path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": FILE_VERSION, "timers": list(self.timers.values())}, f)
        os.replace(tmp, path)

    def _load(self) -> str | None:
        """Load the file. Returns a notice to speak if it was unreadable."""
        path = self._path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            items = data["timers"]
            self.timers = {t["id"]: t for t in items if isinstance(t, dict)}
            for t in self.timers.values():
                t["due"] = float(t["due"])
                t["state"] = t.get("state", "pending")
        except (OSError, ValueError, KeyError, TypeError):
            self.timers = {}
            try:
                os.replace(path, path + ".bad")
            except OSError:
                pass
            return "Your saved timers file could not be read. I kept a copy and started with no timers."
        return None

    async def load_and_announce(self) -> None:
        notice = self._load()
        now = self.clock()
        missed = []
        for t in self.timers.values():
            if t["state"] == "awaiting" or (t["state"] == "pending" and t["due"] <= now):
                t["state"] = "missed"
            if t["state"] == "missed" and not t.get("announced"):
                t["announced"] = True
                missed.append(t)
        if self.timers:
            self._save()
        if notice:
            await self._say(notice)
        if missed:
            names = "; ".join(f"{self._label(t)} (was due {self._plain(t['due'])})" for t in missed)
            await self._say(
                f"{len(missed)} timer{'s' if len(missed) != 1 else ''} came due while Wingman was "
                f"closed and did not run: {names}. Say run missed timer to run one, or cancel it."
            )

    # ---------------------------------------------------------- helpers
    async def _say(self, text: str) -> None:
        self.log.info(text)
        await self.wingman.conversation.add_assistant(text)
        await self.wingman.tts.speak(text, priority=SpeechPriority.EVENT)

    def _plain(self, epoch: float) -> str:
        dt = datetime.fromtimestamp(epoch).astimezone()
        now = datetime.fromtimestamp(self.clock()).astimezone()
        day = "today" if dt.date() == now.date() else (
            "tomorrow" if dt.date() == (now + timedelta(days=1)).date() else dt.strftime("%b %d"))
        return f"{dt.strftime('%I:%M %p').lstrip('0')} {day}"

    @staticmethod
    def _label(t: dict) -> str:
        a = t["action"]
        text = a.get("args", {}).get("message") or a.get("args", {}).get("request") or a["name"]
        return str(text)[:60]

    def _needs_confirm(self, t: dict) -> bool:
        a = t["action"]
        if a["type"] == "prompt":
            return bool(self._prop("confirm_prompt_actions", True))
        if a["type"] != "tool":
            return False
        names = [a["name"].lower()]
        if a["name"] == "execute_command":
            names.append(str(a.get("args", {}).get("command_name", "")).lower())
        return any(w in n for w in self._confirm_words() for n in names)

    def _new_id(self) -> str:
        pool = string.ascii_letters + string.digits
        while True:
            tid = "".join(random.choice(pool) for _ in range(7))
            if tid not in self.timers:
                return tid

    # ------------------------------------------------------------ tools
    @tool(description="Set a timer to run an action after a delay (seconds) or at a clock time. "
          "Give exactly one of delay or at_time ('20:00', '8:00 PM' or ISO 8601, local time). "
          "function_name is a tool or command name, 'remind_me' (arguments {\"message\": ...}) or "
          "'prompt' (arguments {\"request\": ...}) to run a stored request later. "
          "Supports loops. Use silent for quick loops.")
    async def set_timer(
        self,
        function_name: str,
        function_arguments_json: str = "{}",
        delay: Optional[float] = None,
        at_time: Optional[str] = None,
        is_loop: bool = False,
        loop_counts: int = -1,
        silent: bool = False,
    ) -> str:
        """
        Args:
            function_name: Tool/command to run, 'remind_me' or 'prompt'.
            function_arguments_json: Arguments as a JSON object.
            delay: Delay in seconds (use this or at_time).
            at_time: Local clock time such as '20:00', '8:00 PM' or ISO 8601.
            is_loop: Whether the timer repeats every delay seconds.
            loop_counts: Number of runs for a loop (-1 for infinite).
            silent: Whether to suppress spoken output.
        """
        if (delay is None) == (not at_time):
            return "Error: give exactly one of delay or at_time."
        if "." in function_name:
            function_name = function_name.split(".")[1]
        if isinstance(function_arguments_json, dict):
            args = function_arguments_json
        elif not function_arguments_json:
            args = {}
        else:
            try:
                args = json.loads(function_arguments_json)
            except (json.JSONDecodeError, TypeError):
                return "Error: Invalid JSON in function_arguments_json."
        if not isinstance(args, dict):
            return "Error: function_arguments_json must be a JSON object."

        if function_name == REMIND:
            action = {"type": "reminder", "name": REMIND, "args": args}
        elif function_name == PROMPT:
            action = {"type": "prompt", "name": PROMPT, "args": args}
        else:
            if not self.wingman.tools.has(function_name):
                if self.wingman.commands.get(function_name):
                    args = {"command_name": function_name}
                    function_name = "execute_command"
                else:
                    return f"Error: Function '{function_name}' does not exist."
            action = {"type": "tool", "name": function_name, "args": args}

        now = self.clock()
        if at_time:
            try:
                due_dt = parse_at_time(at_time, datetime.fromtimestamp(now).astimezone())
            except ValueError as e:
                return f"Error: {e}"
            due = due_dt.timestamp()
            interval = max(1, int(delay)) if delay else 86400
            spoken = f"Set for {self._plain(due)}."
        else:
            if delay <= 0:
                return "Error: Delay must be greater than 0."
            interval = max(1, int(delay))
            due = now + interval
            spoken = f"Set for {interval} seconds from now."
        for other in self.timers.values():
            if (other["state"] == "pending" and other["action"] == action
                    and other["is_loop"] == bool(is_loop) and abs(other["due"] - due) < 60):
                return (f"Error: an identical timer is already set for {self._plain(other['due'])} "
                        f"(id {other['id']}). Cancel it first if you want a new one.")
        timer = {"id": self._new_id(), "due": due, "interval": interval, "is_loop": bool(is_loop),
                 "loops": int(loop_counts), "silent": bool(silent), "action": action,
                 "created": now, "state": "pending"}
        self.timers[timer["id"]] = timer
        self._save()
        return f"Timer set with id {timer['id']}. {spoken}\n\n{await self.get_timer_status()}"

    @tool(description="Get a list of all timers: pending, missed while closed, and awaiting confirmation. Keep IDs to yourself.")
    async def get_timer_status(self) -> list[dict]:
        """List timers with plain local times."""
        now = self.clock()
        out = []
        for t in self.timers.values():
            row = {"id": t["id"], "what": self._label(t), "state": t["state"],
                   "is_loop": t["is_loop"],
                   "remaining_loops": (t["loops"] if t["loops"] > 0 else "infinite") if t["is_loop"] else "N/A"}
            if t["state"] == "pending":
                row["due"] = self._plain(t["due"])
                row["remaining_time_in_seconds"] = max(0, int(t["due"] - now))
            elif t["state"] == "missed":
                row["was_due"] = self._plain(t["due"])
            elif t["state"] == "awaiting":
                row["waiting_for"] = "the player to say confirm timer"
            out.append(row)
        return out

    @tool(description="Cancel a timer (pending, missed or awaiting) by its id.")
    async def cancel_timer(self, id: str) -> str:
        """
        Args:
            id: The id of the timer to cancel.
        """
        if not id or id not in self.timers:
            return f"Timer with id '{str(id)}' not found."
        self.timers.pop(id)
        self._save()
        return f"Timer with id {id} cancelled.\n\n{await self.get_timer_status()}"

    @tool(description="Change a timer's settings.")
    async def change_timer_settings(
        self,
        id: str,
        delay: Optional[float] = None,
        is_loop: Optional[bool] = None,
        loops: Optional[int] = None,
        silent: Optional[bool] = None,
    ) -> str:
        """
        Args:
            id: The id of the timer to change.
            delay: The new delay in seconds (restarts the countdown).
            is_loop: Whether the timer should loop.
            loops: Number of remaining loops (-1 for infinite).
            silent: Whether the timer should be silent.
        """
        t = self.timers.get(id) if id else None
        if not t:
            return f"Timer with id '{str(id)}' not found."
        if delay is not None:
            t["interval"] = max(1, int(delay))
            if t["state"] == "pending":
                t["due"] = self.clock() + t["interval"]
        if is_loop is not None:
            t["is_loop"] = bool(is_loop)
        if loops is not None:
            t["loops"] = int(loops)
        if silent is not None:
            t["silent"] = bool(silent)
        self._save()
        return f"Timer with id '{id}' settings have been changed.\n\n{await self.get_timer_status()}"

    @tool(description="Remind the user with a message. Use as 'function_name' in 'set_timer' when the user says 'remind me to...'.")
    async def remind_me(self, message: str) -> str:
        """
        Args:
            message: The message to remind the user with.
        """
        return message or "This is your reminder, no message was given."

    @tool(description="Run a timer that came due while Wingman was closed, when the player asks for it.")
    async def run_missed_timer(self, id: str) -> str:
        """
        Args:
            id: The id of the missed timer.
        """
        t = self.timers.get(id) if id else None
        if not t or t["state"] != "missed":
            return f"No missed timer with id '{str(id)}'."
        await self._execute(t, self.clock())
        return "Missed timer run."

    @tool(description="Confirm a timer that is waiting to press keys or run a stored request, when the player says 'confirm timer'.")
    async def confirm_timer(self, id: str = "") -> str:
        """
        Args:
            id: The timer id; may be left empty when exactly one timer is waiting.
        """
        now = self.clock()
        waiting = [t for t in self.timers.values() if t["state"] == "awaiting"]
        t = self.timers.get(id) if id else (waiting[0] if len(waiting) == 1 else None)
        if not t or t["state"] != "awaiting":
            return "No timer is waiting for confirmation."
        if now >= t["deadline"]:
            await self._skip(t, now)
            return "That timer was not confirmed in time and was skipped."
        await self._execute(t, now)
        return "Timer confirmed and run."

    # ---------------------------------------------------------- engine
    async def tick(self) -> None:
        now = self.clock()
        for t in list(self.timers.values()):
            if t["state"] == "pending" and t["due"] <= now:
                if self._needs_confirm(t):
                    t["state"] = "awaiting"
                    t["deadline"] = now + float(self._prop("confirm_window_seconds", 60))
                    self._save()
                    what = "run its stored request" if t["action"]["type"] == "prompt" \
                        else f"use {t['action']['name']}"
                    await self._say(f"Timer {self._label(t)} wants to {what}. Say confirm timer to do it.")
                else:
                    await self._execute(t, now)
            elif t["state"] == "awaiting" and now >= t["deadline"]:
                await self._skip(t, now)

    def _advance(self, t: dict, now: float) -> None:
        if not t["is_loop"] or t["loops"] == 1:
            self.timers.pop(t["id"], None)
        else:
            t["due"] = now + t["interval"]
            t["state"] = "pending"
            t["announced"] = False
            t.pop("deadline", None)
            if t["loops"] > 0:
                t["loops"] -= 1
        self._save()

    async def _skip(self, t: dict, now: float) -> None:
        await self._say(f"Timer {self._label(t)} was not confirmed and was skipped.")
        self._advance(t, now)

    async def _execute(self, t: dict, now: float) -> None:
        # Advance first so a failing action can never re-fire every tick.
        a = dict(t["action"])
        silent = t["silent"]
        self._advance(t, now)
        try:
            if a["type"] == "reminder":
                msg = a["args"].get("message") or "This is your reminder, no message was given."
                if not silent:
                    await self._say(str(msg))
            elif a["type"] == "prompt":
                await self._run_prompt(str(a["args"].get("request", "")), silent)
            else:
                result = await self.wingman.tools.invoke(a["name"], a["args"])
                response = getattr(result, "instant_response", None) or getattr(result, "response", None)
                if response and not silent:
                    await self._say(await self._summarize(a, str(response)))
        except Exception as e:
            self.log.error(f"Timer action failed: {e}", server_only=True)

    async def _run_prompt(self, request: str, silent: bool) -> None:
        process = None
        try:  # upstream's Wingman.process is not on the skill facade; use it only if exposed
            process = getattr(self.wingman, "process", None)
        except Exception:
            process = None
        if callable(process):
            await process(transcript=request)
            return
        gap = "prompt timers run as a spoken reminder: no public full-turn call on the skill API"
        if gap not in self.gaps:
            self.gaps.append(gap)
        if not silent:
            await self._say(f"Timer reminder: {request}")

    async def _summarize(self, action: dict, response: str) -> str:
        try:
            summary = await self.wingman.ai.generate(
                f'Timed "{action["name"]}" with "{action["args"]}" was executed. Create a small '
                f"summary of what was executed, by meaning, not as a function call. "
                f"Write it in {self.wingman.language.name}.\nThe function response:\n```\n{response}\n```",
                auto_shorten=True,
            )
            return summary or response
        except Exception:
            return response
