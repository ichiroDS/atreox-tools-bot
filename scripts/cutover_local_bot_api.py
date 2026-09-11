"""One-time move from the official Bot API to our local Bot API server.

Telegram requires a bot to call ``logOut`` against the *official* Bot API
before it is served by a local server. After that call the official API will
not accept the token again for 10 minutes, so this is a deliberate, one-way
step - and this script refuses to take it by accident:

* Without ``--execute`` it is a dry run: it only checks.
* With ``--execute`` it still asks for confirmation unless ``--yes`` is given.
* Every check runs before ``logOut``; any failure aborts with nothing changed.

Steps
    1. required settings exist (token, local URL; file URL recommended)
    2. the local Bot API server - and its file endpoint - answer
    3. the token is valid on the official API (getMe)
    4. logOut on the official API                    (--execute only)
    5. getMe through the local server, same bot id   (--execute only)

It must run where the local server is reachable, i.e. inside the Railway
project. From a machine with the Railway CLI::

    railway ssh --service atreox-tools-bot -- python /app/scripts/cutover_local_bot_api.py \\
        --local-url http://telegram-bot-api.railway.internal:8081 \\
        --files-url http://telegram-bot-api.railway.internal:8082

and, once the dry run is clean, the same command with ``--execute --yes``.
The printed next steps then switch the bot service's variables over.

The token is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import OFFICIAL_BOT_API_URL  # noqa: E402

EXIT_OK = 0
EXIT_PRECHECK_FAILED = 1
EXIT_LOGOUT_FAILED = 2
EXIT_LOCAL_FAILED = 3
EXIT_ABORTED = 4

# The local server authorises the bot on its first request, which can take a
# little while; getMe is retried for about a minute.
_LOCAL_GETME_ATTEMPTS = 12
_LOCAL_GETME_DELAY = 5.0


class Transport(Protocol):
    async def get(self, url: str) -> tuple[int, Any]: ...

    async def post(self, url: str) -> tuple[int, Any]: ...


class AiohttpTransport:
    """Real HTTP. Any HTTP response - even a 404 - proves the server is up."""

    def __init__(self, timeout: float = 20.0) -> None:
        self._timeout = timeout

    async def _request(self, method: str, url: str) -> tuple[int, Any]:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url) as response:
                try:
                    payload = await response.json(content_type=None)
                except Exception:  # noqa: BLE001 - non-JSON bodies are fine here
                    payload = None
                return response.status, payload

    async def get(self, url: str) -> tuple[int, Any]:
        return await self._request("GET", url)

    async def post(self, url: str) -> tuple[int, Any]:
        return await self._request("POST", url)


@dataclass
class Plan:
    token: str
    local_url: str
    files_url: str | None
    official_url: str = OFFICIAL_BOT_API_URL


class CutoverError(Exception):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def redact(text: str, token: str) -> str:
    return text.replace(token, "<BOT_TOKEN>") if token else text


def build_plan(args: argparse.Namespace, env: dict[str, str]) -> Plan:
    """Collect and validate settings. Raises before anything touches the network."""
    token = (env.get("BOT_TOKEN") or "").strip()
    if not token or ":" not in token:
        raise CutoverError("BOT_TOKEN is missing or malformed", EXIT_PRECHECK_FAILED)

    local_url = (args.local_url or env.get("LOCAL_BOT_API_URL") or "").strip()
    if not local_url:
        candidate = (env.get("BOT_API_BASE_URL") or "").strip()
        if candidate and "api.telegram.org" not in candidate:
            local_url = candidate
    local_url = local_url.rstrip("/")
    if not local_url:
        raise CutoverError(
            "no local Bot API URL: pass --local-url (e.g. "
            "http://telegram-bot-api.railway.internal:8081)",
            EXIT_PRECHECK_FAILED,
        )
    if not local_url.startswith(("http://", "https://")):
        raise CutoverError("the local Bot API URL must start with http(s)://", EXIT_PRECHECK_FAILED)
    if "api.telegram.org" in local_url:
        raise CutoverError(
            "the local Bot API URL points at the official API", EXIT_PRECHECK_FAILED
        )

    files_url = (args.files_url or env.get("BOT_API_FILES_URL") or "").strip().rstrip("/") or None
    return Plan(token=token, local_url=local_url, files_url=files_url,
                official_url=args.official_url.rstrip("/"))


class Cutover:
    def __init__(
        self,
        plan: Plan,
        transport: Transport,
        *,
        out: Callable[[str], None] = print,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.plan = plan
        self.http = transport
        self._out = out
        self._sleep = sleep
        self.official_bot: dict | None = None

    def say(self, text: str) -> None:
        self._out(redact(text, self.plan.token))

    def _method(self, base: str, method: str) -> str:
        return f"{base}/bot{self.plan.token}/{method}"

    async def _reachable(self, label: str, url: str) -> None:
        try:
            status, _ = await self.http.get(url + "/")
        except Exception as exc:  # noqa: BLE001 - any transport failure means unreachable
            raise CutoverError(
                f"{label} is not reachable at {url} ({type(exc).__name__}: {exc})",
                EXIT_PRECHECK_FAILED,
            ) from exc
        self.say(f"  ok  {label} answers at {url} (HTTP {status})")

    async def prechecks(self) -> None:
        self.say("1/5 settings")
        self.say(f"  ok  token present, local Bot API = {self.plan.local_url}")
        if self.plan.files_url:
            self.say(f"  ok  file endpoint = {self.plan.files_url}")
        else:
            self.say(
                "  !!  no --files-url: large files will only work if the bot shares "
                "the server's disk. On Railway you want the file endpoint."
            )

        self.say("2/5 local Bot API reachability (no token is sent)")
        await self._reachable("local Bot API", self.plan.local_url)
        if self.plan.files_url:
            await self._reachable("file endpoint", self.plan.files_url)

        self.say("3/5 official Bot API getMe")
        try:
            status, payload = await self.http.get(self._method(self.plan.official_url, "getMe"))
        except Exception as exc:  # noqa: BLE001
            raise CutoverError(f"official getMe failed: {type(exc).__name__}", EXIT_PRECHECK_FAILED) from exc
        if not (isinstance(payload, dict) and payload.get("ok")):
            raise CutoverError(
                f"official getMe refused (HTTP {status}): {_describe(payload)} - "
                "if the bot was already logged out, skip to verifying the local server",
                EXIT_PRECHECK_FAILED,
            )
        self.official_bot = payload["result"]
        self.say(f"  ok  @{self.official_bot.get('username')} (id {self.official_bot.get('id')})")

    async def log_out(self) -> None:
        self.say("4/5 logOut on the OFFICIAL Bot API")
        try:
            status, payload = await self.http.post(self._method(self.plan.official_url, "logOut"))
        except Exception as exc:  # noqa: BLE001
            raise CutoverError(f"logOut request failed: {type(exc).__name__}", EXIT_LOGOUT_FAILED) from exc
        if not (isinstance(payload, dict) and payload.get("ok")):
            raise CutoverError(
                f"logOut refused (HTTP {status}): {_describe(payload)}", EXIT_LOGOUT_FAILED
            )
        self.say("  ok  logged out of api.telegram.org (it refuses this token for 10 minutes)")

    async def verify_local(self) -> dict:
        self.say("5/5 getMe through the local Bot API")
        last = "no answer"
        for attempt in range(1, _LOCAL_GETME_ATTEMPTS + 1):
            try:
                status, payload = await self.http.get(self._method(self.plan.local_url, "getMe"))
                if isinstance(payload, dict) and payload.get("ok"):
                    bot = payload["result"]
                    expected = (self.official_bot or {}).get("id")
                    if expected is not None and bot.get("id") != expected:
                        raise CutoverError(
                            f"local server answered as bot id {bot.get('id')}, expected {expected}",
                            EXIT_LOCAL_FAILED,
                        )
                    self.say(f"  ok  local server serves @{bot.get('username')} (id {bot.get('id')})")
                    return bot
                last = f"HTTP {status}: {_describe(payload)}"
            except CutoverError:
                raise
            except Exception as exc:  # noqa: BLE001 - retried
                last = type(exc).__name__
            self.say(f"  ..  attempt {attempt}/{_LOCAL_GETME_ATTEMPTS}: {last}")
            if attempt < _LOCAL_GETME_ATTEMPTS:
                await self._sleep(_LOCAL_GETME_DELAY)
        raise CutoverError(f"local getMe never succeeded ({last})", EXIT_LOCAL_FAILED)

    def next_steps(self) -> None:
        files = self.plan.files_url or "<file endpoint URL>"
        self.say("")
        self.say("Cutover done. From your machine, point the bot service at the local server:")
        self.say(
            "  railway variable set --service atreox-tools-bot --skip-deploys "
            f"BOT_API_BASE_URL={self.plan.local_url}"
        )
        self.say(
            "  railway variable set --service atreox-tools-bot --skip-deploys "
            f"BOT_API_FILES_URL={files}"
        )
        self.say("  railway service redeploy --service atreox-tools-bot --yes")
        self.say("Startup should log: local_api=True and media limits input=2000MB.")


def _describe(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("description") or payload)
    return str(payload)


def _confirm(bot_username: str | None, reader: Callable[[str], str]) -> bool:
    answer = reader(
        f"This logs @{bot_username} out of api.telegram.org. "
        "The live bot stops receiving updates until it is redeployed on the local server.\n"
        "Type LOGOUT to continue: "
    )
    return answer.strip() == "LOGOUT"


async def run(
    args: argparse.Namespace,
    *,
    env: dict[str, str] | None = None,
    transport: Transport | None = None,
    out: Callable[[str], None] = print,
    reader: Callable[[str], str] = input,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> int:
    env = dict(os.environ if env is None else env)
    try:
        plan = build_plan(args, env)
    except CutoverError as exc:
        out(f"ABORTED: {exc}")
        return exc.exit_code

    cutover = Cutover(plan, transport or AiohttpTransport(), out=out, sleep=sleep)
    try:
        await cutover.prechecks()
        if not args.execute:
            cutover.say("")
            cutover.say("Dry run complete - nothing was changed. Re-run with --execute to log out.")
            return EXIT_OK

        username = (cutover.official_bot or {}).get("username")
        if not args.yes and not _confirm(username, reader):
            cutover.say("ABORTED: not confirmed - nothing was changed.")
            return EXIT_ABORTED

        await cutover.log_out()
        await cutover.verify_local()
    except CutoverError as exc:
        cutover.say(f"FAILED: {exc}")
        if exc.exit_code == EXIT_LOCAL_FAILED:
            cutover.say(
                "The bot is logged out of the official API but the local server did not "
                "answer. Fix the telegram-bot-api service (check its logs and "
                "TELEGRAM_API_ID/TELEGRAM_API_HASH), then re-run with --verify-only. To roll "
                "back instead, wait 10 minutes and redeploy the bot unchanged: it is still "
                "configured for api.telegram.org."
            )
        return exc.exit_code

    cutover.next_steps()
    return EXIT_OK


async def verify_only(args: argparse.Namespace, **kwargs: Any) -> int:
    """Re-check the local server after a cutover, without touching the official API."""
    env = dict(os.environ if kwargs.get("env") is None else kwargs["env"])
    out = kwargs.get("out", print)
    try:
        plan = build_plan(args, env)
    except CutoverError as exc:
        out(f"ABORTED: {exc}")
        return exc.exit_code
    cutover = Cutover(plan, kwargs.get("transport") or AiohttpTransport(), out=out,
                      sleep=kwargs.get("sleep", asyncio.sleep))
    try:
        await cutover._reachable("local Bot API", plan.local_url)
        await cutover.verify_local()
    except CutoverError as exc:
        cutover.say(f"FAILED: {exc}")
        return exc.exit_code
    cutover.next_steps()
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Move the bot from api.telegram.org to the local Bot API server."
    )
    parser.add_argument("--local-url", help="e.g. http://telegram-bot-api.railway.internal:8081")
    parser.add_argument("--files-url", help="e.g. http://telegram-bot-api.railway.internal:8082")
    parser.add_argument("--execute", action="store_true",
                        help="actually call logOut (default is a dry run)")
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    parser.add_argument("--verify-only", action="store_true",
                        help="only check getMe through the local server")
    parser.add_argument("--official-url", default=OFFICIAL_BOT_API_URL, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.verify_only:
        return asyncio.run(verify_only(args))
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
