"""The one-time local Bot API cutover script.

Every test runs against a fake transport: nothing here can reach Telegram, so
no test can ever log the real bot out.
"""

from __future__ import annotations

import pytest

from scripts import cutover_local_bot_api as cutover

TOKEN = "123456789:AAFakeTokenForTestsOnly-0123456789abcdef"
LOCAL = "http://telegram-bot-api.railway.internal:8081"
FILES = "http://telegram-bot-api.railway.internal:8082"
OFFICIAL = "https://api.telegram.org"
BOT = {"id": 123456789, "username": "atreox_tools_bot", "is_bot": True}


class FakeTransport:
    """URL -> (status, payload) or an exception; records every call."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    async def _answer(self, method, url):
        self.calls.append((method, url))
        answer = self.routes.get((method, url), self.routes.get(url))
        if answer is None:
            raise ConnectionError(f"no route to {url}")
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, list):  # successive answers
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer

    async def get(self, url):
        return await self._answer("GET", url)

    async def post(self, url):
        return await self._answer("POST", url)

    def logged_out(self) -> bool:
        return any(url.endswith("/logOut") for _, url in self.calls)


def healthy_routes(overrides=None, **kw_overrides):
    routes = {
        f"{LOCAL}/": (404, {"ok": False, "error_code": 404, "description": "Not Found"}),
        f"{FILES}/": (404, None),
        f"{OFFICIAL}/bot{TOKEN}/getMe": (200, {"ok": True, "result": BOT}),
        ("POST", f"{OFFICIAL}/bot{TOKEN}/logOut"): (200, {"ok": True, "result": True}),
        f"{LOCAL}/bot{TOKEN}/getMe": (200, {"ok": True, "result": BOT}),
    }
    routes.update(overrides or {})
    routes.update(kw_overrides)
    return routes


def args(*extra):
    return cutover.parse_args(["--local-url", LOCAL, "--files-url", FILES, *extra])


async def no_sleep(_):
    return None


async def run(transport, *argv, env=None, reader=lambda prompt: ""):
    lines: list[str] = []
    code = await cutover.run(
        args(*argv),
        env={"BOT_TOKEN": TOKEN} if env is None else env,
        transport=transport,
        out=lines.append,
        reader=reader,
        sleep=no_sleep,
    )
    return code, "\n".join(lines)


async def test_dry_run_checks_everything_and_never_logs_out():
    transport = FakeTransport(healthy_routes())
    code, output = await run(transport)
    assert code == cutover.EXIT_OK
    assert not transport.logged_out()
    assert "Dry run complete" in output
    assert "@atreox_tools_bot" in output


async def test_the_token_is_never_printed():
    transport = FakeTransport(healthy_routes())
    _, output = await run(transport, "--execute", "--yes")
    assert TOKEN not in output


async def test_reachability_checks_do_not_send_the_token_to_the_local_server_first():
    transport = FakeTransport(healthy_routes())
    await run(transport)
    local_calls = [url for _, url in transport.calls if url.startswith(LOCAL)]
    assert local_calls == [f"{LOCAL}/"]


async def test_missing_token_aborts_before_any_request():
    transport = FakeTransport(healthy_routes())
    code, output = await run(transport, "--execute", "--yes", env={})
    assert code == cutover.EXIT_PRECHECK_FAILED
    assert transport.calls == []
    assert "BOT_TOKEN" in output


async def test_missing_local_url_aborts_before_any_request():
    transport = FakeTransport(healthy_routes())
    lines: list[str] = []
    code = await cutover.run(
        cutover.parse_args(["--execute", "--yes"]),
        env={"BOT_TOKEN": TOKEN, "BOT_API_BASE_URL": OFFICIAL},
        transport=transport, out=lines.append, sleep=no_sleep,
    )
    assert code == cutover.EXIT_PRECHECK_FAILED
    assert transport.calls == []


async def test_the_official_api_is_never_accepted_as_the_local_one():
    transport = FakeTransport(healthy_routes())
    code = await cutover.run(
        cutover.parse_args(["--local-url", OFFICIAL, "--execute", "--yes"]),
        env={"BOT_TOKEN": TOKEN}, transport=transport, out=lambda _: None, sleep=no_sleep,
    )
    assert code == cutover.EXIT_PRECHECK_FAILED
    assert not transport.logged_out()


async def test_an_unreachable_local_server_aborts_before_log_out():
    routes = healthy_routes()
    del routes[f"{LOCAL}/"]
    transport = FakeTransport(routes)
    code, output = await run(transport, "--execute", "--yes")
    assert code == cutover.EXIT_PRECHECK_FAILED
    assert not transport.logged_out()
    assert "not reachable" in output


async def test_an_unreachable_file_endpoint_aborts_before_log_out():
    routes = healthy_routes()
    del routes[f"{FILES}/"]
    transport = FakeTransport(routes)
    code, _ = await run(transport, "--execute", "--yes")
    assert code == cutover.EXIT_PRECHECK_FAILED
    assert not transport.logged_out()


async def test_an_invalid_token_aborts_before_log_out():
    transport = FakeTransport(healthy_routes(**{
        f"{OFFICIAL}/bot{TOKEN}/getMe": (401, {"ok": False, "description": "Unauthorized"}),
    }))
    code, _ = await run(transport, "--execute", "--yes")
    assert code == cutover.EXIT_PRECHECK_FAILED
    assert not transport.logged_out()


async def test_execute_without_confirmation_changes_nothing():
    transport = FakeTransport(healthy_routes())
    code, output = await run(transport, "--execute", reader=lambda prompt: "no")
    assert code == cutover.EXIT_ABORTED
    assert not transport.logged_out()
    assert "nothing was changed" in output


async def test_confirmed_cutover_logs_out_officially_then_verifies_locally():
    transport = FakeTransport(healthy_routes())
    code, output = await run(transport, "--execute", reader=lambda prompt: "LOGOUT")
    assert code == cutover.EXIT_OK

    methods = [url.rsplit("/", 1)[-1] for _, url in transport.calls]
    assert methods == ["", "", "getMe", "logOut", "getMe"]
    # logOut went to the official API, as Telegram requires...
    assert ("POST", f"{OFFICIAL}/bot{TOKEN}/logOut") in transport.calls
    # ...and the final getMe went through the local server.
    assert transport.calls[-1] == ("GET", f"{LOCAL}/bot{TOKEN}/getMe")
    assert "BOT_API_BASE_URL=" + LOCAL in output
    assert "BOT_API_FILES_URL=" + FILES in output


async def test_local_getme_is_retried_while_the_server_authorises():
    transport = FakeTransport(healthy_routes(**{
        f"{LOCAL}/bot{TOKEN}/getMe": [
            (502, None),
            (200, {"ok": True, "result": BOT}),
        ],
    }))
    code, _ = await run(transport, "--execute", "--yes")
    assert code == cutover.EXIT_OK


async def test_a_local_server_answering_as_another_bot_fails():
    other = {**BOT, "id": 1}
    transport = FakeTransport(healthy_routes(**{
        f"{LOCAL}/bot{TOKEN}/getMe": (200, {"ok": True, "result": other}),
    }))
    code, output = await run(transport, "--execute", "--yes")
    assert code == cutover.EXIT_LOCAL_FAILED
    assert "roll" in output  # rollback guidance is printed


async def test_a_refused_log_out_is_reported():
    transport = FakeTransport(healthy_routes({
        ("POST", f"{OFFICIAL}/bot{TOKEN}/logOut"): (400, {"ok": False, "description": "nope"}),
    }))
    code, _ = await run(transport, "--execute", "--yes")
    assert code == cutover.EXIT_LOGOUT_FAILED


async def test_verify_only_never_touches_the_official_api():
    transport = FakeTransport(healthy_routes())
    code = await cutover.verify_only(
        args("--verify-only"), env={"BOT_TOKEN": TOKEN}, transport=transport,
        out=lambda _: None, sleep=no_sleep,
    )
    assert code == cutover.EXIT_OK
    assert all(not url.startswith(OFFICIAL) for _, url in transport.calls)


@pytest.mark.parametrize("flag", ["--execute", "--yes", "--verify-only"])
def test_flags_default_to_off(flag):
    parsed = cutover.parse_args([])
    assert getattr(parsed, flag.lstrip("-").replace("-", "_")) is False
