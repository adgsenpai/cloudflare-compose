from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

import httpx


def name(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", value):
        raise ValueError("Use a 1–64 character name containing letters, digits, dots, underscores or hyphens")
    return value


def cf_id(value: str) -> str:
    if not re.fullmatch(r"[a-fA-F0-9]{32}", value):
        raise ValueError("Expected a 32-character Cloudflare identifier")
    return value


def remote_path(value: str) -> str:
    if not value.startswith("/") or ".." in PurePosixPath(value).parts or any(ord(c) < 32 for c in value):
        raise ValueError("Use an absolute remote path without parent traversal or control characters")
    return value


class Registry:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "registry.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS registry (kind TEXT, name TEXT, data TEXT, PRIMARY KEY(kind,name))")
            db.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at TEXT DEFAULT CURRENT_TIMESTAMP, operation TEXT, target TEXT, outcome TEXT)")
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def add(self, kind, key, data):
        name(key)
        try:
            with self.connect() as db:
                db.execute("INSERT INTO registry VALUES (?,?,?)", (kind, key, json.dumps(data)))
        except sqlite3.IntegrityError:
            raise ValueError("Name already registered; choose a new name") from None
        return {"name": key, **data}

    def get(self, kind, key):
        with self.connect() as db:
            row = db.execute("SELECT data FROM registry WHERE kind=? AND name=?", (kind, key)).fetchone()
        if row is None:
            raise ValueError(f"Unknown {kind}: {key}")
        return json.loads(row[0])

    def list(self, kind):
        with self.connect() as db:
            return [{"name": k, **json.loads(v)} for k, v in db.execute("SELECT name,data FROM registry WHERE kind=? ORDER BY name", (kind,))]

    def audit(self, operation, target, outcome):
        with self.connect() as db:
            db.execute("INSERT INTO audit(operation,target,outcome) VALUES (?,?,?)", (operation, target, outcome))


class Cloudflare:
    def __init__(self, profile, transport=None):
        self.profile = profile
        self.transport = transport

    def request(self, method, path, **kwargs):
        token = os.environ.get(self.profile["token_env"])
        if not token:
            raise ValueError("Cloudflare token environment variable is not configured")
        try:
            with httpx.Client(base_url="https://api.cloudflare.com/client/v4/", headers={"Authorization": f"Bearer {token}"}, timeout=30, transport=self.transport) as client:
                response = client.request(method, path, **kwargs)
                if response.status_code >= 400:
                    raise ValueError(f"Cloudflare returned HTTP {response.status_code}; check permissions or rate limits")
                body = response.json()
                if not body.get("success"):
                    raise ValueError("Cloudflare rejected the request")
                return {"result": body.get("result"), "result_info": body.get("result_info")}
        except httpx.RequestError:
            raise ValueError("Cloudflare connection failed or timed out") from None

    def check_zone(self, zone):
        result = self.request("GET", f"zones/{cf_id(zone)}")["result"]
        if result.get("account", {}).get("id") != self.profile["account_id"]:
            raise ValueError("Zone does not belong to the selected profile account")


def compose_command(project, action, service=None, tail=100):
    commands = {"status": ["ps", "--format", "json"], "logs": ["logs", "--no-color", "--tail", str(tail)], "validate": ["config", "--quiet"], "pull": ["pull"], "up": ["up", "-d"], "stop": ["stop"], "restart": ["restart"], "down": ["down"]}
    if action not in commands:
        raise ValueError("Unsupported Compose action")
    if not 1 <= tail <= 1000:
        raise ValueError("Log tail must be between 1 and 1000")
    args = ["docker", "compose", "--project-directory", project["directory"], "-p", project["project_name"], "-f", project["compose_file"], *commands[action]]
    if service:
        if action in ("down", "validate"):
            raise ValueError("This action does not accept a service")
        args.append(name(service))
    return shlex.join(args)


def ssh_args(machine, command):
    return ["ssh", "-F", "/dev/null", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", "-o", f'UserKnownHostsFile={machine["known_hosts"]}', "-i", machine["identity_file"], "-p", str(machine["port"]), "-l", machine["user"], machine["host"], command]


def run_ssh(machine, command):
    # File-backed capture prevents remote output from exhausting process memory.
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(ssh_args(machine, command), stdout=output, stderr=subprocess.STDOUT, timeout=120, check=False)
        except subprocess.TimeoutExpired:
            return {"exit_code": None, "outcome": "unknown", "output": "SSH timed out. Remote work may still be running; inspect status before retrying."}
        except FileNotFoundError:
            raise ValueError("OpenSSH client is not installed") from None
        size = output.tell()
        output.seek(0)
        return {"exit_code": result.returncode, "outcome": "success" if result.returncode == 0 else "failed", "output": output.read(32768).decode(errors="replace"), "truncated": size > 32768}
