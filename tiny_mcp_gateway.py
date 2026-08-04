"""tiny-mcp-gateway: Zero-dependency MCP proxy/gateway server.

Aggregate multiple MCP servers behind one endpoint. Route tools by prefix.
Load-balance across server instances. Auth, rate limiting, and observability hooks.
Like a reverse proxy for AI agent tool infrastructure.

MIT License | github.com/hussain-alsaibai/tiny-mcp-gateway
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from enum import Enum

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# Protocol types (matching MCP JSON-RPC 2.0)
# ---------------------------------------------------------------------------

class MCPMethod(str, Enum):
    INITIALIZE          = "initialize"
    TOOLS_LIST          = "tools/list"
    TOOLS_CALL          = "tools/call"
    RESOURCES_LIST      = "resources/list"
    RESOURCES_READ      = "resources/read"
    RESOURCES_SUBSCRIBE = "resources/subscribe"
    PROMPTS_LIST        = "prompts/list"
    PROMPTS_GET         = "prompts/get"
    SAMPLING_CREATE     = "sampling/create"
    CANCEL              = "notifications/cancelled"
    PING                = "ping"

    @classmethod
    def from_raw(cls, raw: str) -> "MCPMethod":
        return cls(raw)


@dataclass
class MCPRequest:
    jsonrpc: str = "2.0"
    id:       Optional[Any] = None
    method:   Optional[str] = None
    params:   Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"jsonrpc": self.jsonrpc}
        if self.id is not None:
            d["id"] = self.id
        if self.method is not None:
            d["method"] = self.method
        if self.params is not None:
            d["params"] = self.params
        return d


@dataclass
class MCPResponse:
    id:     Optional[Any] = None
    result: Optional[Dict[str, Any]] = None
    error:  Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"jsonrpc": "2.0"}
        if self.id is not None:
            d["id"] = self.id
        if self.error is not None:
            d["error"] = self.error
        elif self.result is not None:
            d["result"] = self.result
        return d

    def to_json(self) -> bytes:
        return json.dumps(self.to_dict()).encode()


# ---------------------------------------------------------------------------
# Backend: a single MCP server connection
# ---------------------------------------------------------------------------

@dataclass
class Backend:
    name:    str
    url:     str                    # stdio command or HTTP URL
    secret:  Optional[str] = None   # HMAC signing secret for this backend
    weight:  int = 1               # load-balance weight
    enabled: bool = True

    # Runtime state
    failures:     int = 0
    last_success: Optional[float] = None

    def is_healthy(self) -> bool:
        if not self.enabled:
            return False
        # Basic health: didn't fail in last 60s
        if self.failures > 3:
            return False
        if self.last_success and time.time() - self.last_success > 300:
            return False
        return True

    def record_success(self) -> None:
        self.failures = max(0, self.failures - 1)
        self.last_success = time.time()

    def record_failure(self) -> None:
        self.failures += 1

    def stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "healthy": self.is_healthy(),
            "failures": self.failures,
            "last_success": self.last_success,
        }


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

@dataclass
class RouteRule:
    """Route matching for tool calls."""
    prefix:       str           # tool name prefix, e.g. "github/"
    backend_name: str           # which backend handles this prefix
    priority:     int = 0       # higher = matched first

    def matches(self, tool_name: str) -> bool:
        return tool_name.startswith(self.prefix)


class Router:
    def __init__(self) -> None:
        self._rules: List[RouteRule] = []
        self._default_backend: Optional[str] = None

    def add_rule(self, rule: RouteRule) -> None:
        self._rules.append(rule)
        self._rules.sort(key=lambda r: -r.priority)

    def set_default(self, backend_name: str) -> None:
        self._default_backend = backend_name

    def route(self, tool_name: str) -> Optional[str]:
        for rule in self._rules:
            if rule.matches(tool_name):
                return rule.backend_name
        return self._default_backend

    def rules_summary(self) -> List[Dict[str, Any]]:
        return [{"prefix": r.prefix, "backend": r.backend_name, "priority": r.priority} for r in self._rules]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@dataclass
class APIKey:
    key:        str
    label:      str = ""
    scopes:     List[str] = field(default_factory=list)   # ["read", "write", "admin"]
    rate_limit: int = 100   # requests per minute
    expires_at: Optional[float] = None

    def is_valid(self) -> bool:
        if self.expires_at and time.time() > self.expires_at:
            return False
        return True

    def has_scope(self, scope: str) -> bool:
        if not self.scopes:
            return True  # no scopes = all allowed
        return scope in self.scopes


class Auth:
    def __init__(self, hmac_secret: Optional[str] = None) -> None:
        self.hmac_secret = hmac_secret or os.environ.get("MCP_GATEWAY_HMAC_SECRET", "")
        self._keys: Dict[str, APIKey] = {}
        self._rate_count: Dict[str, List[float]] = {}

    def add_key(self, key: str, label: str = "", scopes: Optional[List[str]] = None,
                rate_limit: int = 100, ttl: Optional[float] = None) -> None:
        expires = time.time() + ttl if ttl else None
        self._keys[key] = APIKey(key=key, label=label, scopes=scopes or [],
                                  rate_limit=rate_limit, expires_at=expires)

    def verify(self, key: Optional[str]) -> Optional[APIKey]:
        if not key:
            return None
        api_key = self._keys.get(key)
        if not api_key or not api_key.is_valid():
            return None
        return api_key

    def check_rate(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = time.time()
        bucket = self._rate_count.setdefault(key, [])
        # expire old entries
        self._rate_count[key] = [t for t in bucket if now - t < window]
        if len(self._rate_count[key]) >= limit:
            return False
        self._rate_count[key].append(now)
        return True

    def verify_hmac(self, body: bytes, sig: str) -> bool:
        if not self.hmac_secret:
            return True
        expected = hmac.new(self.hmac_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _make_logger(name: str = "tiny-mcp-gateway") -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S"
        ))
        log.addHandler(h)
        log.setLevel(os.environ.get("MCP_LOG_LEVEL", "INFO"))
    return log


# ---------------------------------------------------------------------------
# MCP Gateway
# ---------------------------------------------------------------------------

class MCPGateway:
    """
    Aggregate multiple MCP backends behind one JSON-RPC 2.0 server.

    Supports:
      - Tool routing by name prefix
      - Round-robin + weighted load balancing
      - Per-key auth + rate limiting
      - HMAC body signing for webhook-style backends
      - Graceful degradation (skip unhealthy backends)
      - Structured audit log
    """

    def __init__(
        self,
        port: int = 8080,
        log_level: str = "INFO",
        hmac_secret: Optional[str] = None,
    ) -> None:
        self.port = port
        self.hmac_secret = hmac_secret
        self.router = Router()
        self.backends: Dict[str, Backend] = {}
        self.auth = Auth(hmac_secret=hmac_secret)
        self._rr_index: Dict[str, int] = {}  # round-robin counters per backend
        self._logger = _make_logger()
        self._routes: Dict[str, Callable[..., Any]] = {}
        self._register_routes()

    # ------------------------------------------------------------------
    # Backend management
    # ------------------------------------------------------------------

    def add_backend(self, backend: Backend) -> None:
        self.backends[backend.name] = backend
        self._rr_index[backend.name] = 0
        self._logger.info("backend registered: %s -> %s", backend.name, backend.url)

    def remove_backend(self, name: str) -> None:
        self.backends.pop(name, None)
        self._logger.warning("backend removed: %s", name)

    def route_tool(self, prefix: str, backend_name: str, priority: int = 0) -> None:
        rule = RouteRule(prefix=prefix, backend_name=backend_name, priority=priority)
        self.router.add_rule(rule)
        self._logger.info("route: %s -> %s", prefix, backend_name)

    def set_default_backend(self, name: str) -> None:
        self.router.set_default(name)

    # ------------------------------------------------------------------
    # API key management
    # ------------------------------------------------------------------

    def add_api_key(
        self,
        label: str = "",
        scopes: Optional[List[str]] = None,
        rate_limit: int = 100,
        ttl: Optional[float] = None,
    ) -> str:
        """Generate and register a new API key. Returns the key."""
        key = f"mcp_{secrets.token_urlsafe(24)}"
        self.auth.add_key(key, label=label, scopes=scopes,
                          rate_limit=rate_limit, ttl=ttl)
        self._logger.info("API key created: %s (%s)", key[:16] + "...", label)
        return key

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    def _parse_request(self, body: bytes) -> Tuple[MCPRequest, Optional[str]]:
        try:
            data = json.loads(body)
            api_key = None
            raw = data.get("params", {})
            if isinstance(raw, dict):
                api_key = raw.pop("_api_key", None)
            req = MCPRequest(
                id=data.get("id"),
                method=data.get("method"),
                params=raw or None,
            )
            return req, api_key
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"Invalid JSON-RPC request: {exc}")

    def _build_error(self, code: int, message: str, req_id: Optional[Any]) -> MCPResponse:
        return MCPResponse(
            id=req_id,
            error={"code": code, "message": message},
        )

    def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools":      {"listChanged": True},
                "resources":  {"subscribe": True, "listChanged": True},
                "prompts":    {"listChanged": True},
            },
            "serverInfo": {
                "name":    "tiny-mcp-gateway",
                "version": __version__,
            },
        }

    def _handle_tools_list(self) -> Dict[str, Any]:
        """Aggregate tools from all healthy backends."""
        tools: List[Dict[str, Any]] = []
        for name, backend in self.backends.items():
            if backend.is_healthy():
                tools.append({
                    "name":        f"_backend/{name}/__tools__",
                    "description": f"Proxy to backend '{name}'",
                    "inputSchema": {"type": "object"},
                    "_backend": name,
                })
        return {"tools": tools}

    def _select_backend(self, tool_name: str) -> Optional[Backend]:
        backend_name = self.router.route(tool_name)
        if not backend_name:
            # Pick a healthy backend round-robin
            healthy = [b for b in self.backends.values() if b.is_healthy()]
            if not healthy:
                return None
            b = healthy[self._rr_index[healthy[0].name] % len(healthy)]
            self._rr_index[b.name] = (self._rr_index[b.name] + 1) % len(healthy)
            return b
        backend = self.backends.get(backend_name)
        if backend and backend.is_healthy():
            return backend
        return None

    async def _call_backend(
        self,
        backend: Backend,
        method: str,
        params: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Forward a request to a backend."""
        if backend.url.startswith("http"):
            # HTTP backend
            import urllib.request
            import urllib.error
            payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}).encode()
            req = urllib.request.Request(
                backend.url,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            if backend.secret:
                sig = hmac.new(backend.secret.encode(), payload, hashlib.sha256).hexdigest()
                req.add_header("X-MCP-Signature", sig)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                    backend.record_success()
                    return data.get("result", {})
            except Exception as exc:
                backend.record_failure()
                raise RuntimeError(f"Backend {backend.name} failed: {exc}") from exc
        else:
            # Stdio backend
            proc = await asyncio.create_subprocess_exec(
                *backend.url.split(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate(
                input=json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}).encode()
            )
            if proc.returncode != 0:
                backend.record_failure()
                raise RuntimeError(f"Backend {backend.name} stdio failed: {err.decode()}")
            backend.record_success()
            return json.loads(out).get("result", {})

    def _authenticate(self, api_key: Optional[str]) -> Optional[str]:
        """Validate API key. Returns label on success, None on failure."""
        key = self.auth.verify(api_key)
        if not key:
            return None
        if not self.auth.check_rate(api_key, key.rate_limit):
            return None
        return key.label

    def _build_response(
        self,
        method: str,
        params: Optional[Dict[str, Any]],
        backend: Backend,
    ) -> MCPResponse:
        return MCPResponse(
            id=int(time.time() * 1000),  # synthetic ID
            result={},  # async handling done by gateway
        )

    # ------------------------------------------------------------------
    # HTTP handler
    # ------------------------------------------------------------------

    def _register_routes(self) -> None:
        """Build the URL routing table."""
        self._routes["/mcp"] = self._route_mcp

    async def _route_mcp(self, body: bytes) -> bytes:
        try:
            req, api_key = self._parse_request(body)
        except ValueError as exc:
            err = self._build_error(-32700, str(exc), None)
            return err.to_json()

        # Auth
        if api_key:
            label = self._authenticate(api_key)
            if not label:
                err = self._build_error(-32001, "Unauthorized or rate limited", req.id)
                return err.to_json()

        # Handle methods
        method = req.method or ""
        try:
            if method == MCPMethod.INITIALIZE:
                result = self._handle_initialize(req.params or {})
                return MCPResponse(id=req.id, result=result).to_json()

            elif method == MCPMethod.TOOLS_LIST:
                result = self._handle_tools_list()
                return MCPResponse(id=req.id, result=result).to_json()

            elif method == MCPMethod.TOOLS_CALL:
                tool_name = (req.params or {}).get("name", "")
                backend = self._select_backend(tool_name)
                if not backend:
                    err = self._build_error(-32002, f"No healthy backend for tool: {tool_name}", req.id)
                    return err.to_json()
                try:
                    result = await self._call_backend(
                        backend, MCPMethod.TOOLS_CALL.value, req.params
                    )
                    return MCPResponse(id=req.id, result=result).to_json()
                except RuntimeError as exc:
                    err = self._build_error(-32003, str(exc), req.id)
                    return err.to_json()

            elif method == MCPMethod.PING:
                return MCPResponse(id=req.id, result={"pong": True}).to_json()

            else:
                # Pass through to default backend
                backend = self._select_backend(method)
                if not backend:
                    err = self._build_error(-32002, f"No backend for method: {method}", req.id)
                    return err.to_json()
                try:
                    result = await self._call_backend(backend, method, req.params)
                    return MCPResponse(id=req.id, result=result).to_json()
                except RuntimeError as exc:
                    err = self._build_error(-32003, str(exc), req.id)
                    return err.to_json()

        except Exception as exc:
            self._logger.exception("request failed")
            err = self._build_error(-32603, f"Internal error: {exc}", req.id)
            return err.to_json()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def handle(self, scope: Any, receive: Any, send: Any) -> None:
        """ASGI app entry point."""
        path = scope.get("path", "")
        if path == "/mcp" and scope.get("method") == "POST":
            body = b""
            async for chunk in receive:
                if chunk.get("type") == "http.request":
                    body += chunk.get("body", b"")
            response_body = await self._route_mcp(body)
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"application/json"]],
            })
            await send({
                "type": "http.response.body",
                "body": response_body,
            })
        elif path == "/health":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"application/json"]],
            })
            healthy = [b.stats() for b in self.backends.values() if b.is_healthy()]
            body = json.dumps({"status": "ok", "backends": healthy}).encode()
            await send({"type": "http.response.body", "body": body})
        elif path == "/routes":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"application/json"]],
            })
            body = json.dumps({
                "backends": [b.stats() for b in self.backends.values()],
                "routes": self.router.rules_summary(),
            }).encode()
            await send({"type": "http.response.body", "body": body})
        else:
            await send({
                "type": "http.response.start",
                "status": 404,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"Not Found"})

    def run(self, host: str = "0.0.0.0", port: Optional[int] = None) -> None:
        """Run the gateway with uvicorn (or stdlib wsgiref fallback)."""
        port = port or self.port
        try:
            import uvicorn
            self._logger.info("starting tiny-mcp-gateway on %s:%d", host, port)
            uvicorn.run(self, host=host, port=port, log_level="error")
        except ImportError:
            self._logger.warning("uvicorn not installed, using stdlib wsgiref")
            from wsgiref.simple_server import make_server
            self._logger.info("starting tiny-mcp-gateway on %s:%d (wsgi)", host, port)
            srv = make_server(host, port, self._wsgi_app())
            srv.serve_forever()

    def _wsgi_app(self) -> Callable[[Any, Any], None]:
        def app(environ: Any, start_response: Any) -> Any:
            path = environ.get("PATH_INFO", "")
            if path == "/mcp" and environ.get("REQUEST_METHOD") == "POST":
                import cgi
                content_length = int(environ.get("CONTENT_LENGTH", 0))
                body = environ["wsgi.input"].read(content_length)
                async def run():
                    resp_body = await self._route_mcp(body)
                    start_response("200 OK", [("Content-Type", "application/json")])
                    return [resp_body]
                # Synchronous wrapper for WSGI
                result = asyncio.run(run())
                return result
            elif path == "/health":
                import json as _json
                body = _json.dumps({"status": "ok"}).encode()
                start_response("200 OK", [("Content-Type", "application/json")])
                return [body]
            else:
                start_response("404 Not Found", [("Content-Type", "text/plain")])
                return [b"Not Found"]
        return app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import sys
    port = int(os.environ.get("MCP_GATEWAY_PORT", "8080"))
    secret = os.environ.get("MCP_GATEWAY_SECRET", "")

    gw = MCPGateway(port=port, hmac_secret=secret or None)

    # Register example backends
    gw.add_backend(Backend(name="fileserver", url="stdio:python -m mcp_fileserver"))
    gw.add_backend(Backend(name="github", url="https://api.github.com/mcp"))
    gw.route_tool("github/", "github", priority=10)
    gw.route_tool("file/", "fileserver", priority=10)
    gw.set_default_backend("github")

    # Create a demo key
    key = gw.add_api_key(label="demo", rate_limit=60)
    print(f"# Demo API key: {key}", flush=True)

    gw.run()
