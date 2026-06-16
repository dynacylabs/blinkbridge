"""Simple web UI for BlinkBridge utility endpoints."""

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

from aiohttp import web


log = logging.getLogger(__name__)


class BlinkBridgeWebServer:
    """Serve utility endpoints such as Frigate camera snippet export."""

    def __init__(self, host: str, port: int, frigate_export_path: Path):
        self.host = host
        self.port = port
        self.frigate_export_path = frigate_export_path
        self._runner = None
        self._site = None
        self._2fa_future: Optional[asyncio.Future] = None
        self._credentials_future: Optional[asyncio.Future] = None
        self.restart_callback: Optional[Callable[[], Awaitable[None]]] = None

    async def request_credentials(self) -> Dict[str, str]:
        """Wait for Blink login credentials submitted via the web UI at POST /login.

        Creates a Future resolved when the user submits the login form.
        """
        loop = asyncio.get_event_loop()
        self._credentials_future = loop.create_future()
        log.info("Blink credentials required — open the web UI at /login to enter them")
        try:
            return await self._credentials_future
        finally:
            self._credentials_future = None

    async def request_2fa_code(self) -> str:
        """Wait for a 2FA code to be submitted via the web UI at POST /2fa.

        Creates a Future that is resolved when the user submits the form.
        Logs a prominent message so the operator knows where to look.
        """
        loop = asyncio.get_event_loop()
        self._2fa_future = loop.create_future()
        log.info("2FA required — open the web UI at /2fa to enter your Blink verification code")
        try:
            return await self._2fa_future
        finally:
            self._2fa_future = None

    async def start(self) -> None:
        app = web.Application()
        app.add_routes([
            web.get("/", self.handle_index),
            web.get("/frigate-cameras.yml", self.handle_frigate_yaml),
            web.get("/health", self.handle_health),
            web.get("/2fa", self.handle_2fa_get),
            web.post("/2fa", self.handle_2fa_post),
            web.get("/login", self.handle_login_get),
            web.post("/login", self.handle_login_post),
            web.post("/restart", self.handle_restart),
        ])

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self.host, port=self.port)
        await self._site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def handle_frigate_yaml(self, _request: web.Request) -> web.Response:
        if not self.frigate_export_path.exists():
            return web.Response(
                text="Frigate camera snippet has not been generated yet.\n",
                status=404,
                content_type="text/plain",
            )
        return web.Response(text=self.frigate_export_path.read_text(), content_type="text/plain")

    async def handle_2fa_get(self, _request: web.Request) -> web.Response:
        pending = self._2fa_future is not None and not self._2fa_future.done()
        if pending:
            body = """
      <p>Blink requires a two-factor authentication code.
         Check your email or SMS and enter it below.</p>
      <form method="post" action="/2fa">
        <label for="code"><strong>Verification code:</strong></label><br /><br />
        <input id="code" name="code" type="text" inputmode="numeric"
               pattern="[0-9]*" autocomplete="one-time-code"
               style="font-size:1.4rem;padding:.4rem .6rem;width:12rem;letter-spacing:.15rem;"
               autofocus required />
        <button type="submit"
                style="margin-left:.75rem;padding:.4rem 1rem;font-size:1rem;">Submit</button>
      </form>"""
            status_text = "Waiting for code"
        else:
            body = "<p>No 2FA verification is currently pending.</p>"
            status_text = "No pending request"

        html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>BlinkBridge — 2FA</title>
    <style>
      body {{ font-family: sans-serif; margin: 2rem; background: #f6f7f9; color: #1f2937; }}
      .card {{ background: white; border-radius: 10px; padding: 1rem 1.25rem; box-shadow: 0 2px 8px rgba(0,0,0,.08); max-width: 480px; }}
    </style>
  </head>
  <body>
    <div class="card">
      <h1>Blink 2FA</h1>
      <p>Status: <strong>{status_text}</strong></p>
      {body}
      <p style="margin-top:1.5rem"><a href="/">&larr; Back</a></p>
    </div>
  </body>
</html>
"""
        return web.Response(text=html, content_type="text/html")

    async def handle_2fa_post(self, request: web.Request) -> web.Response:
        data = await request.post()
        code = str(data.get("code", "")).strip()
        if not code:
            return web.Response(text="Missing 'code' field.", status=400, content_type="text/plain")
        if self._2fa_future is None or self._2fa_future.done():
            return web.Response(text="No 2FA request is currently pending.", status=409, content_type="text/plain")
        self._2fa_future.set_result(code)
        html = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>BlinkBridge — 2FA submitted</title>
    <style>
      body { font-family: sans-serif; margin: 2rem; background: #f6f7f9; color: #1f2937; }
      .card { background: white; border-radius: 10px; padding: 1rem 1.25rem; box-shadow: 0 2px 8px rgba(0,0,0,.08); max-width: 480px; }
    </style>
  </head>
  <body>
    <div class="card">
      <h1>Code submitted</h1>
      <p>Your verification code has been sent to Blink. You can close this page.</p>
      <p><a href="/">&larr; Back</a></p>
    </div>
  </body>
</html>
"""
        return web.Response(text=html, content_type="text/html")

    async def handle_login_get(self, _request: web.Request) -> web.Response:
        pending = self._credentials_future is not None and not self._credentials_future.done()
        if pending:
            body = """
      <p>Enter your Blink account credentials to authenticate.</p>
      <form method="post" action="/login" autocomplete="on">
        <div style="margin-bottom:.75rem;">
          <label for="username"><strong>Username (email):</strong></label><br />
          <input id="username" name="username" type="email" autocomplete="username"
                 style="font-size:1rem;padding:.4rem .6rem;width:20rem;" autofocus required />
        </div>
        <div style="margin-bottom:.75rem;">
          <label for="password"><strong>Password:</strong></label><br />
          <input id="password" name="password" type="password" autocomplete="current-password"
                 style="font-size:1rem;padding:.4rem .6rem;width:20rem;" required />
        </div>
        <button type="submit" style="padding:.4rem 1rem;font-size:1rem;">Sign in</button>
      </form>"""
            status_text = "Waiting for credentials"
        else:
            body = "<p>No login request is currently pending.</p>"
            status_text = "No pending request"

        html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>BlinkBridge &#8212; Sign in</title>
    <style>
      body {{ font-family: sans-serif; margin: 2rem; background: #f6f7f9; color: #1f2937; }}
      .card {{ background: white; border-radius: 10px; padding: 1rem 1.25rem; box-shadow: 0 2px 8px rgba(0,0,0,.08); max-width: 520px; }}
    </style>
  </head>
  <body>
    <div class="card">
      <h1>Blink Sign In</h1>
      <p>Status: <strong>{status_text}</strong></p>
      {body}
      <p style="margin-top:1.5rem"><a href="/">&larr; Back</a></p>
    </div>
  </body>
</html>
"""
        return web.Response(text=html, content_type="text/html")

    async def handle_login_post(self, request: web.Request) -> web.Response:
        data = await request.post()
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        if not username or not password:
            return web.Response(text="Missing 'username' or 'password'.", status=400, content_type="text/plain")
        if self._credentials_future is None or self._credentials_future.done():
            return web.Response(text="No login request is currently pending.", status=409, content_type="text/plain")
        self._credentials_future.set_result({"username": username, "password": password})
        html = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>BlinkBridge &#8212; Signed in</title>
    <style>
      body { font-family: sans-serif; margin: 2rem; background: #f6f7f9; color: #1f2937; }
      .card { background: white; border-radius: 10px; padding: 1rem 1.25rem; box-shadow: 0 2px 8px rgba(0,0,0,.08); max-width: 520px; }
    </style>
  </head>
  <body>
    <div class="card">
      <h1>Credentials submitted</h1>
      <p>Your credentials have been sent to Blink. You can close this page.</p>
      <p><a href="/">&larr; Back</a></p>
    </div>
  </body>
</html>
"""
        return web.Response(text=html, content_type="text/html")

    async def handle_restart(self, _request: web.Request) -> web.Response:
        if self.restart_callback is None:
            return web.Response(text="Restart not available.", status=503, content_type="text/plain")
        # Fire the restart as a background task so we can return the response first.
        asyncio.create_task(self.restart_callback())
        html = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta http-equiv="refresh" content="8; url=/" />
    <title>BlinkBridge &#8212; Restarting</title>
    <style>
      body { font-family: sans-serif; margin: 2rem; background: #f6f7f9; color: #1f2937; }
      .card { background: white; border-radius: 10px; padding: 1rem 1.25rem; box-shadow: 0 2px 8px rgba(0,0,0,.08); max-width: 480px; }
    </style>
  </head>
  <body>
    <div class="card">
      <h1>Restarting&hellip;</h1>
      <p>BlinkBridge is restarting. You will be redirected to the home page in a moment.</p>
      <p><a href="/">&larr; Back</a></p>
    </div>
  </body>
</html>
"""
        return web.Response(text=html, content_type="text/html")

    async def handle_index(self, request: web.Request) -> web.Response:
        raw_url = str(request.url.with_path("/frigate-cameras.yml").with_query(None).with_fragment(None))
        if self.frigate_export_path.exists():
            content = self.frigate_export_path.read_text()
            status = "ready"
        else:
            content = "# Frigate camera snippet has not been generated yet.\n"
            status = "waiting"

        twofa_pending = self._2fa_future is not None and not self._2fa_future.done()
        creds_pending = self._credentials_future is not None and not self._credentials_future.done()
        banners = ""
        if creds_pending:
            banners += (
                '<div style="background:#fee2e2;border:1px solid #ef4444;border-radius:8px;'
                'padding:.75rem 1rem;margin-bottom:.75rem;">'
                '&#9888; Blink credentials required &mdash; '
                '<a href="/login"><strong>sign in</strong></a></div>'
            )
        if twofa_pending:
            banners += (
                '<div style="background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;'
                'padding:.75rem 1rem;margin-bottom:.75rem;">'
                '&#9888; Blink 2FA required &mdash; '
                '<a href="/2fa"><strong>enter your verification code</strong></a></div>'
            )

        html = f"""<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>BlinkBridge Utilities</title>
    <style>
      body {{ font-family: sans-serif; margin: 2rem; background: #f6f7f9; color: #1f2937; }}
      .card {{ background: white; border-radius: 10px; padding: 1rem 1.25rem; box-shadow: 0 2px 8px rgba(0,0,0,.08); }}
      pre {{ background: #111827; color: #e5e7eb; padding: 1rem; border-radius: 8px; overflow: auto; max-height: 60vh; }}
      .meta {{ margin-bottom: .75rem; }}
      .btn-restart {{ background: #e5e7eb; border: 1px solid #9ca3af; border-radius: 6px; padding: .35rem .85rem; font-size: .9rem; cursor: pointer; }}
      .btn-restart:hover {{ background: #d1d5db; }}
    </style>
  </head>
  <body>
    <div class=\"card\">
      {banners}
      <h1>BlinkBridge Frigate Export</h1>
      <p class=\"meta\">Status: <strong>{status}</strong></p>
      <p class=\"meta\">Raw endpoint: <a href=\"/frigate-cameras.yml\">/frigate-cameras.yml</a></p>
      <pre>{content}</pre>
      <form method=\"post\" action=\"/restart\" style=\"margin-top:1rem\"
            onsubmit=\"return confirm('Restart BlinkBridge now?');\">
        <button type=\"submit\" class=\"btn-restart\">&#8635; Restart</button>
      </form>
    </div>
  </body>
</html>
"""
        return web.Response(text=html, content_type="text/html")
