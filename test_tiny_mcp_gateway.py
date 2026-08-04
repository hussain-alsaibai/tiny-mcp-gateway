"""Tests for tiny-mcp-gateway."""
import asyncio, json, tempfile, shutil, unittest
from tiny_mcp_gateway import (
    MCPGateway, Backend, MCPRequest, MCPResponse, MCPMethod,
    Router, RouteRule, Auth, APIKey,
)

class TestMCPRequestResponse(unittest.TestCase):
    def test_request_to_dict(self):
        req = MCPRequest(id=1, method="tools/list", params={"a": 1})
        d = req.to_dict()
        self.assertEqual(d["jsonrpc"], "2.0")
        self.assertEqual(d["id"], 1)
        self.assertEqual(d["method"], "tools/list")
        self.assertEqual(d["params"]["a"], 1)

    def test_response_to_dict_result(self):
        r = MCPResponse(id=1, result={"tools": []})
        self.assertEqual(r.to_dict()["result"], {"tools": []})

    def test_response_to_dict_error(self):
        r = MCPResponse(id=1, error={"code": -32600, "message": "Invalid Request"})
        self.assertIn("error", r.to_dict())
        self.assertNotIn("result", r.to_dict())

    def test_response_to_json(self):
        r = MCPResponse(id=1, result={"ok": True})
        self.assertEqual(json.loads(r.to_json()), r.to_dict())


class TestBackend(unittest.TestCase):
    def test_healthy_default(self):
        b = Backend(name="test", url="https://example.com")
        self.assertTrue(b.is_healthy())

    def test_unhealthy_after_failures(self):
        b = Backend(name="test", url="https://example.com", failures=5)
        self.assertFalse(b.is_healthy())

    def test_record_success(self):
        b = Backend(name="test", url="https://example.com", failures=3)
        b.record_success()
        self.assertEqual(b.failures, 2)

    def test_record_failure(self):
        b = Backend(name="test", url="https://example.com", failures=0)
        b.record_failure()
        self.assertEqual(b.failures, 1)


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.r = Router()

    def test_add_rule(self):
        self.r.add_rule(RouteRule("github/", "gh", priority=10))
        self.r.add_rule(RouteRule("file/", "fs", priority=5))
        self.assertEqual(self.r.route("github/repo"), "gh")
        self.assertEqual(self.r.route("file/read"), "fs")

    def test_priority_order(self):
        self.r.add_rule(RouteRule("x", "low", priority=1))
        self.r.add_rule(RouteRule("x", "high", priority=10))
        self.assertEqual(self.r.route("x"), "high")

    def test_default_fallback(self):
        self.r.set_default("default-backend")
        self.assertEqual(self.r.route("unknown/tool"), "default-backend")


class TestAuth(unittest.TestCase):
    def test_add_and_verify_key(self):
        auth = Auth()
        auth.add_key("test-key", label="test", scopes=["read"], rate_limit=10)
        key = auth.verify("test-key")
        self.assertIsNotNone(key)
        self.assertEqual(key.label, "test")

    def test_invalid_key(self):
        auth = Auth()
        self.assertIsNone(auth.verify("bad-key"))

    def test_rate_limit(self):
        auth = Auth()
        auth.add_key("rate-key", rate_limit=3)
        for _ in range(3):
            self.assertTrue(auth.check_rate("rate-key", 3, window=60))
        self.assertFalse(auth.check_rate("rate-key", 3, window=60))

    def test_expired_key(self):
        auth = Auth()
        auth.add_key("exp-key", ttl=-1)  # already expired
        self.assertIsNone(auth.verify("exp-key"))


class TestMCPGateway(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.gw = MCPGateway(port=8080)
        self.gw.add_backend(Backend(name="test", url="https://example.com"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gateway_initialization(self):
        self.assertEqual(self.gw.port, 8080)
        self.assertIn("test", self.gw.backends)

    def test_tools_list_response(self):
        result = self.gw._handle_tools_list()
        self.assertIn("tools", result)
        self.assertGreater(len(result["tools"]), 0)

    def test_initialize_response(self):
        result = self.gw._handle_initialize({})
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertIn("capabilities", result)

    def test_route_mcp_request(self):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}}
        }).encode()
        import asyncio
        result = asyncio.run(self.gw._route_mcp(body))
        data = json.loads(result)
        self.assertIn("result", data)
        self.assertEqual(data["result"]["serverInfo"]["name"], "tiny-mcp-gateway")

    def test_route_ping(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        import asyncio
        result = asyncio.run(self.gw._route_mcp(body))
        data = json.loads(result)
        self.assertEqual(data["result"]["pong"], True)

    def test_route_invalid_json(self):
        import asyncio
        result = asyncio.run(self.gw._route_mcp(b"not json"))
        data = json.loads(result)
        self.assertIn("error", data)

    def test_add_api_key(self):
        key = self.gw.add_api_key(label="test-agent", rate_limit=50)
        self.assertTrue(key.startswith("mcp_"))
        self.assertIsNotNone(self.gw.auth.verify(key))

    def test_route_rules_summary(self):
        self.gw.route_tool("gh/", "test", priority=5)
        summary = self.gw.router.rules_summary()
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["prefix"], "gh/")

    def test_backend_selection(self):
        self.gw.route_tool("api/", "test", priority=10)
        self.gw.add_backend(Backend(name="fallback", url="https://fallback.com"))
        self.gw.set_default_backend("fallback")
        backend = self.gw._select_backend("api/foo")
        self.assertEqual(backend.name, "test")


if __name__ == "__main__":
    unittest.main()
