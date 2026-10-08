import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from cloudflare_compose import server
from cloudflare_compose.core import Cloudflare, Registry, compose_command, remote_path, run_ssh, ssh_args


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CF_COMPOSE_HOME", str(tmp_path / "state"))
    return server.registry()


def test_registry_persistence_duplicates_and_permissions(db):
    db.add("profile", "work", {"account_id": "a" * 32, "token_env": "CF_WORK_TOKEN"})
    assert Registry(db.path.parent).get("profile", "work")["token_env"] == "CF_WORK_TOKEN"
    assert db.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="already"):
        db.add("profile", "work", {})


@pytest.mark.parametrize("path", ["relative", "/srv/../etc", "/srv/hello\nworld"])
def test_remote_path_validation(path):
    with pytest.raises(ValueError):
        remote_path(path)


def test_shell_quoting():
    p = {"directory": "/srv/a; touch /tmp/pwn", "compose_file": "/srv/a $(whoami)/compose.yaml", "project_name": "web"}
    args = shlex.split(compose_command(p, "up"))
    assert args[3] == p["directory"]
    assert args[7] == p["compose_file"]
    with pytest.raises(ValueError):
        compose_command(p, "up", "--build")


def test_machine_options_and_rejection(db, tmp_path):
    key = tmp_path / "key"; key.touch()
    hosts = tmp_path / "hosts"; hosts.touch()
    with pytest.raises(ValueError):
        server.machine_add("x", "-oProxyCommand=evil", "ops", str(key), str(hosts))
    m = server.machine_add("x", "host.example", "ops", str(key), str(hosts))
    args = ssh_args(m, "docker compose version")
    assert "StrictHostKeyChecking=yes" in args
    assert args[1:3] == ["-F", "/dev/null"]


def test_mutation_preview_does_not_execute(db, monkeypatch):
    db.add("machine", "host", {})
    server.project_add("web", "host", "/srv/web", "/srv/web/compose.yaml", "web")
    def forbidden(*a):
        raise AssertionError("unexpected SSH")
    monkeypatch.setattr(server, "run_ssh", forbidden)
    assert server.compose_run("web", "down")["preview"]
    with pytest.raises(ValueError):
        server.project_add("bad", "host", "/srv/web", "/srv/other/file", "web")


def test_compose_execution_audited(db, monkeypatch):
    db.add("machine", "host", {})
    server.project_add("web", "host", "/srv/web", "/srv/web/compose.yaml", "web")
    monkeypatch.setattr(server, "run_ssh", lambda *a: {"outcome": "success", "exit_code": 0})
    assert server.compose_run("web", "up", execute=True)["exit_code"] == 0
    with db.connect() as con:
        assert con.execute("SELECT outcome FROM audit ORDER BY id").fetchall() == [("started",), ("success",)]


def test_account_isolation_and_token_redaction(monkeypatch):
    monkeypatch.setenv("CF_TEST_TOKEN", "secret-value")
    def handler(request):
        assert request.headers["authorization"] == "Bearer secret-value"
        return httpx.Response(200, json={"success": True, "result": {"account": {"id": "b" * 32}}})
    cf = Cloudflare({"account_id": "a" * 32, "token_env": "CF_TEST_TOKEN"}, httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="does not belong"):
        cf.check_zone("c" * 32)
    cf.transport = httpx.MockTransport(lambda r: httpx.Response(403, text="secret-value"))
    with pytest.raises(ValueError) as error:
        cf.request("GET", "zones")
    assert "secret-value" not in str(error.value)


def test_dns_preview_and_write(db, monkeypatch):
    calls = []
    class FakeCF:
        def check_zone(self, z):
            calls.append(("zone", z))
        def request(self, *args, **kwargs):
            calls.append((args, kwargs)); return {"result": {"id": "d" * 32}}
    monkeypatch.setattr(server, "client", lambda p: FakeCF())
    args = dict(profile="work", zone_id="a" * 32, operation="create", record_name="app.example.com", content="192.0.2.1")
    assert server.cloudflare_dns_write(**args)["preview"]
    assert len(calls) == 1
    server.cloudflare_dns_write(**args, execute=True)
    assert calls[-1][0][0] == "POST"
    with pytest.raises(ValueError, match="MX"):
        server.cloudflare_dns_write(**{**args, "record_type": "MX"})


def test_timeout_is_unknown(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("ssh", 120)
    monkeypatch.setattr(subprocess, "run", timeout)
    machine = dict(known_hosts="/tmp/hosts", identity_file="/tmp/key", port=22, user="ops", host="host")
    assert run_ssh(machine, "docker compose version")["outcome"] == "unknown"


def test_mcp_stdio_round_trip(tmp_path):
    async def check():
        params = StdioServerParameters(command=sys.executable, args=["-m", "cloudflare_compose.server"], env={**os.environ, "CF_COMPOSE_HOME": str(tmp_path / "mcp-state"), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {"inventory", "compose_run", "profile_add", "cloudflare_tunnels"} <= {t.name for t in tools.tools}
                result = await session.call_tool("inventory", {})
                assert not result.isError
                added = await session.call_tool("profile_add", {"profile": "work", "account_id": "a" * 32, "token_env": "CF_WORK_TOKEN"})
                assert not added.isError
                result = await session.call_tool("inventory", {})
                assert "CF_WORK_TOKEN" in result.content[0].text
    asyncio.run(check())
