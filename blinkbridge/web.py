"""Simple web UI for BlinkBridge utility endpoints."""

from pathlib import Path

from aiohttp import web


class BlinkBridgeWebServer:
    """Serve utility endpoints such as Frigate camera snippet export."""

    def __init__(self, host: str, port: int, frigate_export_path: Path):
        self.host = host
        self.port = port
        self.frigate_export_path = frigate_export_path
        self._runner = None
        self._site = None

    async def start(self) -> None:
        app = web.Application()
        app.add_routes([
            web.get("/", self.handle_index),
            web.get("/frigate-cameras.yml", self.handle_frigate_yaml),
            web.get("/health", self.handle_health),
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

    async def handle_index(self, request: web.Request) -> web.Response:
        raw_url = str(request.url.with_path("/frigate-cameras.yml").with_query(None).with_fragment(None))
        if self.frigate_export_path.exists():
            content = self.frigate_export_path.read_text()
            status = "ready"
        else:
            content = "# Frigate camera snippet has not been generated yet.\n"
            status = "waiting"

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
    </style>
  </head>
  <body>
    <div class=\"card\">
      <h1>BlinkBridge Frigate Export</h1>
      <p class=\"meta\">Status: <strong>{status}</strong></p>
      <p class=\"meta\">Raw endpoint: <a href=\"/frigate-cameras.yml\">/frigate-cameras.yml</a></p>
      <pre>{content}</pre>
    </div>
  </body>
</html>
"""
        return web.Response(text=html, content_type="text/html")
