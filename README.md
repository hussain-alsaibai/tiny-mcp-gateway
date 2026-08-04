# tiny-mcp-gateway

> Zero-dependency MCP proxy/gateway server. Aggregate multiple MCP servers, route by tool prefix, load-balance, and enforce auth + rate limits. Like a reverse proxy for AI agent tool infrastructure.

```bash
pip install tiny-mcp-gateway   # coming soon
```

## Why?

- **Multiple MCP servers** — Claude Desktop, Cursor, and custom servers all need separate clients
- **No aggregation layer** — no standard way to expose multiple MCP servers behind one endpoint
- **No routing** — can't route `github/*` tools to a GitHub backend and `file/*` to a filesystem backend

**tiny-mcp-gateway** is one file that solves all three: proxy, router, load balancer, auth gateway — zero deps.

## Quick Start

```python
from tiny_mcp_gateway import MCPGateway, Backend

gw = MCPGateway(port=8080)

# Register backends
gw.add_backend(Backend(name="github",   url="https://api.github.com/mcp"))
gw.add_backend(Backend(name="files",     url="stdio:python -m mcp_fileserver"))
gw.add_backend(Backend(name="database",  url="https://db.internal/mcp"))

# Route by tool prefix
gw.route_tool("github/", "github",   priority=10)
gw.route_tool("file/",   "files",    priority=10)
gw.route_tool("db/",      "database", priority=10)
gw.set_default_backend("github")

# Generate an API key
key = gw.add_api_key(label="my-agent", rate_limit=100, scopes=["read"])

print(f"Gateway running. Key: {key}")

# Run the server
gw.run()  # starts on port 8080
```

## Making Requests

```bash
curl -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "github/repo",
      "arguments": {"owner": "openai", "repo": "GPT-5"}
    }
  }'
```

## Auth & Rate Limiting

```python
# Create keys with different scopes and rate limits
gw.add_api_key(label="read-only-agent", scopes=["read"], rate_limit=50)
gw.add_api_key(label="full-agent",      scopes=["read", "write"], rate_limit=200)

# Pass key in params (works with all backends)
curl -X POST http://localhost:8080/mcp \
  -d '{"method": "tools/call", "params": {"name": "github/repo", "_api_key": "mcp_xxx"}}'
```

## HMAC Signing (for webhook backends)

```python
import os

# Set HMAC secret (shared with your backend)
os.environ["MCP_GATEWAY_HMAC_SECRET"] = "super-secret-key"

gw = MCPGateway(hmac_secret=os.environ["MCP_GATEWAY_HMAC_SECRET"])
gw.add_backend(Backend(
    name="secure-service",
    url="https://secure.internal/mcp",
    secret="super-secret-key",  # backend verifies HMAC signature
))
gw.run()
```

## Architecture

```
Clients                          tiny-mcp-gateway
  |                                    |
  |-- POST /mcp (auth)  -----------> [Auth] --> [Router] --> [Backend Pool]
  |   tools/call github/repo               |            |
  |                                         v            v
  |-- GET  /health ---------> [Health]     [GitHub MCP]  [Files MCP]
  |-- GET  /routes --------> [Route Map]   [DB MCP]
```

## Backend Types

### HTTP Backend
```python
gw.add_backend(Backend(name="api", url="https://api.example.com/mcp"))
```

### Stdio Backend (local subprocess)
```python
gw.add_backend(Backend(
    name="files",
    url="stdio:npx @anthropic/mcp-server-filesystem ./data"
))
gw.add_backend(Backend(
    name="github",
    url="stdio:python -m mcp_github --token $GITHUB_TOKEN"
))
```

## Health & Monitoring

```bash
# Health check
curl http://localhost:8080/health
# {"status": "ok", "backends": [{"name": "github", "healthy": true, "failures": 0}]}

# Route map
curl http://localhost:8080/routes
# {"backends": [...], "routes": [{"prefix": "github/", "backend": "github", "priority": 10}]}
```

## ASGI / WSGI Compatible

Works with any ASGI server (uvicorn, hypercorn) or as a plain Python module:

```python
# uvicorn
import uvicorn
from tiny_mcp_gateway import MCPGateway
app = MCPGateway(port=8080)
uvicorn.run(app, host="0.0.0.0", port=8080)

# Or run directly
app.run()
```

## AI Agent Fit

`tiny-mcp-gateway` is the backbone of a multi-tool agent architecture:

- **Single endpoint** — agents connect to one gateway, access all tools
- **Tool routing** — automatic routing by prefix, no manual tool dispatch
- **Load balancing** — weighted round-robin across backend replicas
- **Circuit breaking** — unhealthy backends are skipped automatically
- **Auth enforcement** — per-key rate limits and scopes

This is the architecture that makes `tiny-agent-service` work with external MCP servers:
```
tiny-agent (reasoning) --> tiny-mcp-client --> tiny-mcp-gateway --> [MCP Servers]
                                                    |
                                                    +-> GitHub tools
                                                    +-> Filesystem tools
                                                    +-> Database tools
```

## API Reference

| Method | Description |
|--------|-------------|
| `MCPGateway(port)` | Create gateway |
| `gw.add_backend(Backend)` | Register a backend |
| `gw.route_tool(prefix, backend, priority)` | Add routing rule |
| `gw.set_default_backend(name)` | Set fallback backend |
| `gw.add_api_key(label, scopes, rate_limit, ttl)` | Create API key |
| `gw.run(host, port)` | Start server |
| `gw.handle(scope, receive, send)` | ASGI entry point |

## Ecosystem

Part of the **tiny-*** zero-dependency toolkit for Python agent infrastructure:

- [**tiny-agent**](https://github.com/hussain-alsaibai/tiny-agent) — zero-dep agent framework
- [**tiny-mcp-client**](https://github.com/hussain-alsaibai/tiny-mcp-client) — MCP client (stdio + SSE)
- [**tiny-task**](https://github.com/hussain-alsaibai/tiny-task) — durable task queue
- [**tiny-retry**](https://github.com/hussain-alsaibai/tiny-retry) — retry + circuit breaker
- [**tiny-webhook**](https://github.com/hussain-alsaibai/tiny-webhook) — production webhook handler
- [**fast-cache**](https://github.com/hussain-alsaibai/fast-cache) — LRU + TTL + SWR cache
- [**snapdb**](https://github.com/hussain-alsaibai/snapdb) — lightning-fast embedded DB

All single-file, MIT, zero dependencies. Built by [OpenClaw](https://github.com/hussain-alsaibai).

## License

MIT © 2026 OpenClaw (hussain-alsaibai)
