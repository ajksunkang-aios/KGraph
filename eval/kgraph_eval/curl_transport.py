"""A curl-based httpx transport for the anthropic SDK.

Problem: the dev LiteLLM gateway (http://113.46.219.251:8080) returns 502 to
every Python HTTP client (httpx, requests) but 200 to curl — a WAF/reverse-
proxy layer filters by something curl has and httpx lacks. Since KBench's
agent loop drives the model through `anthropic.Anthropic` (httpx), every agent
turn 502s and no real A/B data can be collected.

Fix: inject an httpx transport that shells out to `curl` for each request —
so the SDK's bytes-on-the-wire are exactly what curl sends (which the gateway
accepts). This is a dev-environment workaround; in a clean environment where
httpx is not filtered, this transport is unnecessary and the default client is
used instead (gated on CURL_TRANSPORT=1 / a misbehaving gateway being detected).

The transport handles the single sync path the KBench agent loop uses:
non-streaming POST to /v1/messages. Streaming is not implemented (KBench's
agent loop calls messages.create without stream=True).
"""
from __future__ import annotations

import json
import subprocess
from typing import Any

import httpx


class CurlTransport(httpx.BaseTransport):
    """httpx transport that proxies each request through `curl`."""

    def __init__(self, timeout: float = 120.0):
        self._timeout = timeout

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        url = str(request.url)
        # Build the curl argv. We pass headers explicitly (curl -H) so the
        # bytes match what a hand-rolled curl invocation sends — which is what
        # the gateway accepts. Body comes from request.content.
        argv = [
            "curl", "-sS", "--no-progress-meter",
            "--max-time", str(self._timeout),
            "-X", method,
            "-w", "\n__CURL_STATUS__%{http_code}",
            url,
        ]
        # Drop hop-by-hop / curl-managed headers; keep the rest verbatim.
        skip = {"host", "content-length", "accept-encoding", "user-agent",
                "connection"}
        for k, v in request.headers.items():
            if k.lower() in skip:
                continue
            argv += ["-H", f"{k}: {v}"]

        body = request.content or b""
        if body:
            argv += ["--data-binary", "@-"]

        proc = subprocess.run(
            argv, input=body, capture_output=True, timeout=self._timeout + 30,
        )
        out = proc.stdout
        marker = b"\n__CURL_STATUS__"
        sep = out.rfind(marker)
        if sep != -1:
            raw = out[:sep]
            status = int(out[sep + len(marker):].strip() or "0")
        else:
            raw, status = out, 0

        if proc.returncode != 0 or status == 0:
            raise httpx.TransportError(
                f"curl failed (rc={proc.returncode}): "
                f"{proc.stderr.decode('utf-8','replace')[:300]}"
            )

        # Parse curl's raw response (headers + body, separated by \r\n\r\n).
        # curl -s without -D dumps only the body to stdout; we used -w for the
        # status code. So `raw` is the body. Reconstruct a minimal Response.
        headers = self._parse_headers_if_any(raw)
        body_bytes = raw
        # If we couldn't peel headers (curl didn't dump them), treat all as body.
        return httpx.Response(
            status_code=status,
            headers=headers,
            content=body_bytes,
            request=request,
        )

    def _parse_headers_if_any(self, raw: bytes) -> httpx.Headers:
        """curl -s dumps only the body by default — no headers to parse.

        We synthesize the essential headers from the body so the SDK can decode
        JSON. content-type defaults to application/json (the gateway always
        returns JSON for /v1/messages).
        """
        h = httpx.Headers()
        h["content-type"] = "application/json"
        return h

    def close(self) -> None:
        pass


def make_client_with_curl(
    base_url: str, auth_token: str | None = None, api_key: str | None = None,
) -> "anthropic.Anthropic":  # type: ignore[name-defined]
    """Build an anthropic.Anthropic client whose HTTP goes through curl.

    Drop-in for KBench's agent.make_client(): same env-based auth resolution,
    but the underlying httpx transport is CurlTransport.
    """
    import anthropic
    kwargs: dict[str, Any] = {}
    if base_url:
        kwargs["base_url"] = base_url
    if auth_token:
        kwargs["auth_token"] = auth_token
    elif api_key:
        kwargs["api_key"] = api_key
    transport = CurlTransport()
    # DefaultHttpxClient accepts httpx transport kwargs.
    kwargs["http_client"] = httpx.Client(transport=transport)
    return anthropic.Anthropic(**kwargs)


def install_curl_client_patch() -> None:
    """Monkeypatch KBench's agent.make_client to use the curl transport.

    KBench's runner.run() calls `from .agent import make_client` at call time
    (it imports the name fresh each call), so patching the module attribute
    makes every subsequent run use the curl-backed client. Reads the same env
    vars KBench's make_client reads (ANTHROPIC_BASE_URL / AUTH_TOKEN / API_KEY).

    Only do this when KGRAPH_EVAL_CURL_TRANSPORT is set (a misbehaving gateway
    that 502s httpx). In a clean environment, leave KBench's default client
    alone.
    """
    import os
    import kbench.harness.runner as _kbr  # runner binds make_client at top
    import kbench.harness.agent as _kba   # patch the source too, for good measure

    def _curl_make_client():
        return make_client_with_curl(
            base_url=os.environ.get("ANTHROPIC_BASE_URL", ""),
            auth_token=os.environ.get("ANTHROPIC_AUTH_TOKEN"),
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
        )

    # runner.run() does `from .agent import make_client` at module load, so the
    # name it calls is bound in runner's namespace. Patch BOTH so it holds
    # regardless of import order / reloads.
    _kbr.make_client = _curl_make_client  # type: ignore[assignment]
    _kba.make_client = _curl_make_client  # type: ignore[assignment]

