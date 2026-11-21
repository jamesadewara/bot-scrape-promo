"""Fetcher classification (2.2, 2.2.1, F15) against a local aiohttp test server. No Jumia traffic."""
import asyncio

import pytest
from aiohttp import web

from promoscout.fetcher import Fetcher, Outcome, detect_block
from promoscout.settings import USER_AGENT, Settings

SEEN_UA = []


@pytest.fixture
async def server(fixture_text):
    if True:
        nodizz = fixture_text("product_nodizz.html")
        challenge = fixture_text("cloudflare_challenge.html")

        async def ok(req):
            SEEN_UA.append(req.headers.get("User-Agent"))
            if req.headers.get("If-None-Match") == '"v1"':
                return web.Response(status=304)
            return web.Response(text=nodizz, content_type="text/html", headers={"ETag": '"v1"'})

        async def slow(req):
            await asyncio.sleep(1.2)
            return web.Response(text=nodizz, content_type="text/html")

        async def truncated(req):
            resp = web.StreamResponse(headers={"Content-Length": str(len(nodizz.encode()))})
            await resp.prepare(req)
            await resp.write(nodizz.encode()[:5000])
            # close early: body shorter than Content-Length
            req.transport.close()
            return resp

        routes = {
            "/ok": ok, "/slow": slow, "/truncated": truncated,
            "/403": lambda r: web.Response(status=403, text="Forbidden"),
            "/429": lambda r: web.Response(status=429, headers={"Retry-After": "30"}),
            "/503": lambda r: web.Response(status=503),
            "/500": lambda r: web.Response(status=500),
            "/404": lambda r: web.Response(status=404),
            "/challenge": lambda r: web.Response(status=503, text=challenge, content_type="text/html",
                                                 headers={"cf-mitigated": "challenge"}),
            "/challenge200": lambda r: web.Response(text=challenge, content_type="text/html"),
            "/redir-chl": lambda r: web.Response(status=302, headers={"Location": "/x?__cf_chl_rt_tk=abc"}),
            "/redir": lambda r: web.Response(status=301, headers={"Location": "/ok"}),
        }

        async def handler(req):
            h = routes[req.path]
            res = h(req)
            return await res if asyncio.iscoroutine(res) else res

        app = web.Application()
        app.router.add_route("GET", "/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


async def fetch(base, path, **kw):
    s = Settings(read_timeout_s=kw.pop("read_timeout_s", 45), connect_timeout_s=5)
    async with Fetcher(s) as f:
        return await f.fetch(base + path, **kw)


async def test_ok_and_user_agent(server):
    r = await fetch(server, "/ok")
    assert r.outcome == Outcome.OK and r.body and r.etag == '"v1"'
    assert SEEN_UA[-1] == USER_AGENT


async def test_conditional_304(server):
    r = await fetch(server, "/ok", etag='"v1"')
    assert r.outcome == Outcome.NOT_MODIFIED and r.body is None


@pytest.mark.parametrize("path,outcome", [
    ("/403", Outcome.BLOCK), ("/challenge", Outcome.BLOCK), ("/challenge200", Outcome.BLOCK),
    ("/redir-chl", Outcome.BLOCK), ("/429", Outcome.PUSHBACK), ("/503", Outcome.PUSHBACK),
    ("/500", Outcome.SERVER_ERROR), ("/404", Outcome.GONE), ("/redir", Outcome.REDIRECT),
])
async def test_classification(server, path, outcome):
    r = await fetch(server, path)
    assert r.outcome == outcome, (path, r.reason)


async def test_retry_after_parsed(server):
    r = await fetch(server, "/429")
    assert r.retry_after_s == 30


async def test_slow_response_waits_for_body(server):
    r = await fetch(server, "/slow", read_timeout_s=10)
    assert r.outcome == Outcome.OK and r.latency_s >= 1.1   # slow is not a failure


async def test_slow_beyond_timeout_is_network_error(server):
    r = await fetch(server, "/slow", read_timeout_s=0.3)
    assert r.outcome == Outcome.NETWORK_ERROR


async def test_truncated_body(server):
    r = await fetch(server, "/truncated")
    assert r.outcome in (Outcome.TRUNCATED, Outcome.NETWORK_ERROR)


async def test_connection_refused_is_network_error():
    r = await fetch("http://127.0.0.1:9", "/x")
    assert r.outcome == Outcome.NETWORK_ERROR


def test_detect_block_ignores_real_pages(fixture_text):
    assert detect_block(200, {}, fixture_text("product_nodizz.html"), "https://www.jumia.com.ng/x", None) is None
