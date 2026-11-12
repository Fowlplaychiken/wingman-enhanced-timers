"""Enhanced Timers: persistent, clock-time, confirm-before-input timers.

Tool names and semantics follow ShipBit's MIT Timer skill (set_timer,
get_timer_status, cancel_timer, change_timer_settings, remind_me).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import random
import string
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

import httpx

from api.enums import SpeechPriority
from skills.skill_base import Skill, tool

if TYPE_CHECKING:
    from api.interface import SettingsConfig, SkillConfig
    from wingmen.wingman_context import WingmanContext

REMIND = "remind_me"
PROMPT = "prompt"
FILE_VERSION = 1
USER_ONLY_TOOLS = {"confirm_timer", "run_missed_timer"}


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
        self.dry_run_log: list[dict] = []
        self._lock = threading.RLock()
        self._thread = None
        self._worker_loop = None
        self._worker_task = None
        self._stopping = False
        self._user_response = ContextVar("timer_user_response", default=False)

    # ------------------------------------------------------------ config
    def _prop(self, prop_id: str, default, allow_empty: bool = False):
        for p in getattr(self.config, "custom_properties", None) or []:
            if p.id == prop_id and p.value is not None and (allow_empty or p.value != ""):
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
        name = self.wingman.name
        if not isinstance(name, str) or not name:
            raise ValueError("A Wingman name or explicit storage_path is required for timer storage.")
        identity = hashlib.sha256(name.encode("utf-8")).hexdigest()
        # Legacy timers.json stays untouched; importing it requires an explicit storage_path.
        return os.path.join(self.get_generated_files_dir(), f"timers-{identity}.json")

    # ------------------------------------------------------- lifecycle
    async def prepare(self) -> None:
        await super().prepare()
        await self.load_and_announce()
        self._stopping = False
        self.active = True
        self._thread = self.wingman.run_in_thread(self.start_timer_worker)
        if self._thread is None:
            self.active = False
            raise RuntimeError("Timer worker could not start.")

    async def unload(self) -> None:
        with self._lock:
            self.active = False
            self._stopping = True
            loop, task = self._worker_loop, self._worker_task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:  # the owned loop already finished
                pass
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 5)
            if self._thread.is_alive():
                raise RuntimeError("Timer worker shutdown is unconfirmed; do not start another worker.")
        await super().unload()

    async def get_prompt(self) -> str | None:
        prompt = await super().get_prompt()
        return f"{prompt or ''}\n\nActive timers:\n{await self.get_timer_status()}"

    async def start_timer_worker(self) -> None:
        with self._lock:
            self._worker_loop = asyncio.get_running_loop()
            self._worker_task = asyncio.current_task()
        try:
            while self.active:
                await asyncio.sleep(max(0.05, float(self._prop("poll_seconds", 2))))
                if not self.active:
                    break
                try:
                    await self.tick()
                except Exception as e:  # keep the worker alive
                    self.log.error(f"Timer tick failed: {e}", server_only=True)
        except asyncio.CancelledError:
            pass  # let the supplier's thread helper close its event loop normally
        finally:
            with self._lock:
                self._worker_loop = self._worker_task = None

    # ------------------------------------------------------ persistence
    def _save(self) -> None:
        with self._lock:
            path = self._path()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": FILE_VERSION, "timers": list(self.timers.values())}, f, allow_nan=False)
            os.replace(tmp, path)

    @staticmethod
    def _valid_timer(t: dict) -> bool:
        if not isinstance(t, dict):
            return False
        a = t.get("action")
        return (isinstance(t.get("id"), str) and bool(t["id"])
                and t.get("state") in {"pending", "missed", "awaiting"}
                and all(type(t.get(k)) in (int, float) and math.isfinite(t[k]) for k in ("due", "interval"))
                and t["interval"] > 0
                and type(t.get("is_loop")) is bool and type(t.get("silent")) is bool
                and type(t.get("loops")) is int and (t["loops"] == -1 or t["loops"] > 0)
                and isinstance(a, dict) and a.get("type") in {"reminder", "tool", "prompt"}
                and isinstance(a.get("name"), str) and bool(a["name"])
                and a["name"] not in USER_ONLY_TOOLS and isinstance(a.get("args"), dict))

    def _load(self) -> str | None:
        """Load the file. Returns a notice to speak if it was unreadable."""
        path = self._path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            items = data["timers"]
            if (data.get("version") != FILE_VERSION or not isinstance(items, list)
                    or not all(self._valid_timer(t) for t in items)
                    or len({t["id"] for t in items}) != len(items)):
                raise ValueError("Invalid saved timer schema or version")
            self.timers = {t["id"]: t for t in items}
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
        if self._user_response.get():
            await self.wingman.tts.speak(text)  # public facade defaults to ANSWER
        else:
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
            command_name = str(a.get("args", {}).get("command_name", ""))
            command = self.wingman.commands.get(command_name)
            if command is None:
                return True  # an unavailable command's effects are unknown
            for action in getattr(command, "actions", None) or []:
                if any(getattr(action, key, None) is not None
                       for key in ("keyboard", "mouse", "joystick", "write", "skill_action")):
                    return True
            names.append(command_name.lower())
            return any(w in n for w in self._confirm_words() for n in names)
        if any(w in n for w in self._confirm_words() for n in names):
            return True
        safe_tools = str(self._prop("unconfirmed_tools", "get_weather", allow_empty=True))
        return a["name"] not in {n.strip() for n in safe_tools.split(",") if n.strip()}

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
        if function_name in USER_ONLY_TOOLS:
            return "Error: confirmation and missed execution require a direct player request."
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
            if self.wingman.commands.get(function_name):
                args = {"command_name": function_name}
                function_name = "execute_command"
            elif not self.wingman.tools.has(function_name):
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
            if not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay <= 0:
                return "Error: Delay must be greater than 0."
            interval = max(1, int(delay))
            due = now + interval
            spoken = f"Set for {interval} seconds from now."
        if type(loop_counts) is not int or (loop_counts != -1 and loop_counts <= 0):
            return "Error: loop_counts must be -1 or a positive integer."
        timer = {"id": self._new_id(), "due": due, "interval": interval, "is_loop": bool(is_loop),
                 "loops": int(loop_counts), "silent": bool(silent), "action": action,
                 "created": now, "state": "pending"}
        with self._lock:
            if self._stopping:
                return "Error: timers are unloading."
            for other in self.timers.values():
                if (other["state"] == "pending" and other["action"] == action
                        and other["is_loop"] == bool(is_loop) and abs(other["due"] - due) < 60):
                    return (f"Error: an identical timer is already set for {self._plain(other['due'])} "
                            f"(id {other['id']}). Cancel it first if you want a new one.")
            self.timers[timer["id"]] = timer
            self._save()
        return f"Timer set with id {timer['id']}. {spoken}\n\n{await self.get_timer_status()}"

    @tool(description="Get a list of all timers: pending, missed while closed, and awaiting confirmation. Keep IDs to yourself.")
    async def get_timer_status(self) -> list[dict]:
        """List timers with plain local times."""
        now = self.clock()
        out = []
        with self._lock:
            timers = [dict(t) for t in self.timers.values()]
        for t in timers:
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
        with self._lock:
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
        if delay is not None and (not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay <= 0):
            return "Error: Delay must be greater than 0."
        if loops is not None and (type(loops) is not int or (loops != -1 and loops <= 0)):
            return "Error: loops must be -1 or a positive integer."
        with self._lock:
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
                t["loops"] = loops
            if silent is not None:
                t["silent"] = bool(silent)
            self._save()
        return f"Timer with id '{id}' settings have been changed.\n\n{await self.get_timer_status()}"

    @tool(description="Show what action and stored-request timers would have done while Dry run actions was enabled.")
    async def get_dry_run_log(self) -> list[dict]:
        """List actions and stored requests recorded during a dry run."""
        return [dict(e) for e in self.dry_run_log]

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
        token = self._user_response.set(True)
        try:
            executed = await self._execute(t, self.clock())
        finally:
            self._user_response.reset(token)
        return "Missed timer handled; check its reported outcome." if executed else "That timer is no longer available."

    @tool(description="Confirm a timer that is waiting to press keys or run a stored request, when the player says 'confirm timer'.")
    async def confirm_timer(self, id: str = "") -> str:
        """
        Args:
            id: The timer id; may be left empty when exactly one timer is waiting.
        """
        now = self.clock()
        with self._lock:
            waiting = [t for t in self.timers.values() if t["state"] == "awaiting"]
            t = self.timers.get(id) if id else (waiting[0] if len(waiting) == 1 else None)
            if not t or t["state"] != "awaiting":
                return "No timer is waiting for confirmation."
            expired = now >= t["deadline"]
        token = self._user_response.set(True)
        try:
            if expired:
                await self._skip(t, now)
                return "That timer was not confirmed in time and was skipped."
            executed = await self._execute(t, now)
        finally:
            self._user_response.reset(token)
        return "Timer confirmation accepted." if executed else "That timer is no longer available."

    # ---------------------------------------------------------- engine
    async def tick(self) -> None:
        now = self.clock()
        with self._lock:
            ids = list(self.timers)
        for tid in ids:
            announce = False
            with self._lock:
                t = self.timers.get(tid)
                if self._stopping or t is None:
                    continue
                if t["state"] == "pending" and t["due"] <= now:
                    if self._needs_confirm(t):
                        t["state"] = "awaiting"
                        t["deadline"] = now + max(1, float(self._prop("confirm_window_seconds", 60)))
                        self._save()
                        announce = True
                    operation = "execute"
                elif t["state"] == "awaiting" and now >= t["deadline"]:
                    operation = "skip"
                else:
                    continue
            if announce:
                what = "run its stored request" if t["action"]["type"] == "prompt" else f"use {t['action']['name']}"
                await self._say(f"Timer {self._label(t)} wants to {what}. Say confirm timer to do it.")
            elif operation == "execute":
                await self._execute(t, now)
            else:
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
        with self._lock:
            if self.timers.get(t["id"]) is not t or t["state"] != "awaiting" or now < t["deadline"]:
                return
            self._advance(t, now)
        await self._say(f"Timer {self._label(t)} was not confirmed and was skipped.")

    async def _execute(self, t: dict, now: float) -> bool:
        # Advance first so a failing action can never re-fire every tick.
        with self._lock:
            if (self._stopping or self.timers.get(t["id"]) is not t
                    or (t["state"] == "pending" and t["due"] > now)):
                return False
            a = dict(t["action"])
            silent = t["silent"]
            due = t["due"]
            dry = (bool(self._prop("dry_run_key_presses", False))
                   and (a["type"] == "prompt" or (a["type"] == "tool" and self._needs_confirm(t))))
            self._advance(t, now)  # claim before any external await; uncertain effects are never retried
        try:
            if dry:
                self.dry_run_log.append({"timer_id": t["id"], "tool": a["name"], "arguments": a["args"],
                                         "due": due, "actual": now, "reason": "dry_run_not_sent"})
                description = "run stored request" if a["type"] == "prompt" else "pressed"
                await self._say(f"Dry run: would have {description} {self._label(t)}")
                return True
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
        return True

    async def _run_prompt(self, request: str, silent: bool) -> None:
        port = self._prop("core_api_port", 49111)
        if type(port) is not int or not 1 <= port <= 65535 or not request.strip():
            await self._say("Stored request was not submitted: set a valid Core API port and a nonempty request.")
            return
        submitted = False
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                                         follow_redirects=False, timeout=10) as client:
                profiles = await client.get("/configs")
                profiles.raise_for_status()
                profile = profiles.json()["current_config_dir"]["name"]
                config = await client.get("/config", params={"config_name": profile})
                config.raise_for_status()
                name = self.wingman.name
                if not name or name not in config.json()["config"]["wingmen"]:
                    raise ValueError("The active Core profile does not contain this Wingman.")
                submitted = True  # any failure after this point may have reached Core
                response = await client.post("/send-text-to-wingman", data={"text": request, "wingman_name": name})
                response.raise_for_status()
            if not silent:
                await self._say("Stored request submitted to Wingman. Its assistant turn has not been verified complete.")
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            notice = ("Stored request outcome is unknown; it will not be retried automatically." if submitted else
                      "Stored request was not submitted: the configured Core API or Wingman profile is unavailable.")
            self.log.error(notice, server_only=True)
            if notice not in self.gaps:
                self.gaps.append(notice)
            await self._say(notice)

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
