from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .core import Cloudflare, Registry, cf_id, compose_command, name, remote_path, run_ssh

mcp = FastMCP("Cloudflare Compose")
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def registry():
    return Registry(Path(os.environ.get("CF_COMPOSE_HOME", "~/.local/share/cloudflare-compose")).expanduser())


def client(profile):
    return Cloudflare(registry().get("profile", profile))


@mcp.tool(annotations=READ)
def inventory() -> dict:
    """List registered profiles, machines and Compose projects, without credential values."""
    db = registry()
    return {kind: db.list(kind) for kind in ("profile", "machine", "project")}


@mcp.tool(annotations=WRITE)
def profile_add(profile: str, account_id: str, token_env: str) -> dict:
    """Register an account profile referencing an existing environment variable, never a token value."""
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", token_env):
        raise ValueError("Use an uppercase environment variable name")
    return registry().add("profile", profile, {"account_id": cf_id(account_id), "token_env": token_env})


@mcp.tool(annotations=WRITE)
def machine_add(machine: str, host: str, user: str, identity_file: str, known_hosts: str, port: int = 22) -> dict:
    """Register an SSH machine. Key and verified known_hosts files must already exist locally."""
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]{0,252}", host):
        raise ValueError("Invalid hostname or IP address")
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{0,63}", user):
        raise ValueError("Invalid SSH user")
    if not 1 <= port <= 65535:
        raise ValueError("Invalid SSH port")
    files = {}
    for key, value in (("identity_file", identity_file), ("known_hosts", known_hosts)):
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"{key} must reference an existing file")
        files[key] = str(path)
    return registry().add("machine", machine, {"host": host, "user": user, "port": port, **files})


@mcp.tool(annotations=WRITE)
def project_add(project: str, machine: str, directory: str, compose_file: str, project_name: str) -> dict:
    """Register an existing remote Compose file. Does not upload files or deploy containers."""
    registry().get("machine", machine)
    directory, compose_file = remote_path(directory), remote_path(compose_file)
    if not Path(compose_file).is_relative_to(directory):
        raise ValueError("Compose file must be inside the project directory")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project_name):
        raise ValueError("Invalid Docker Compose project name")
    return registry().add("project", project, {"machine": machine, "directory": directory, "compose_file": compose_file, "project_name": project_name})


@mcp.tool(annotations=READ)
def machine_check(machine: str) -> dict:
    """Verify SSH connectivity and remote Docker Compose availability."""
    return run_ssh(registry().get("machine", machine), "docker compose version")


@mcp.tool(annotations=WRITE)
def compose_run(project: str, action: Literal["status", "logs", "validate", "pull", "up", "stop", "restart", "down"], service: str | None = None, execute: bool = False, tail: int = 100) -> dict:
    """Inspect or operate a registered Compose project. Mutations default to preview; execute=true applies them. Down preserves volumes. Remote output may contain application secrets."""
    db = registry()
    target = db.get("project", project)
    command = compose_command(target, action, service, tail)
    if action not in ("status", "logs", "validate") and not execute:
        return {"preview": True, "machine": target["machine"], "command": command, "apply": "Repeat with execute=true after reviewing the target and action"}
    db.audit(f"compose.{action}", project, "started")
    result = run_ssh(db.get("machine", target["machine"]), command)
    db.audit(f"compose.{action}", project, result["outcome"])
    return result


@mcp.tool(annotations=READ)
def cloudflare_account(profile: str) -> dict:
    """Fetch the account bound to this profile (requires Account Settings Read)."""
    cf = client(profile)
    return cf.request("GET", f'accounts/{cf.profile["account_id"]}')


@mcp.tool(annotations=READ)
def cloudflare_zones(profile: str, page: int = 1) -> dict:
    """List one page of zones scoped to this account. Follow result_info for further pages."""
    if page < 1:
        raise ValueError("Page must be positive")
    cf = client(profile)
    return cf.request("GET", "zones", params={"account.id": cf.profile["account_id"], "page": page, "per_page": 50})


@mcp.tool(annotations=READ)
def cloudflare_dns_list(profile: str, zone_id: str, page: int = 1) -> dict:
    """List DNS records after verifying the zone belongs to the selected account."""
    if page < 1:
        raise ValueError("Page must be positive")
    cf = client(profile)
    cf.check_zone(zone_id)
    return cf.request("GET", f"zones/{zone_id}/dns_records", params={"page": page, "per_page": 100})


@mcp.tool(annotations=WRITE)
def cloudflare_dns_write(profile: str, zone_id: str, operation: Literal["create", "update", "delete"], record_id: str | None = None, record_type: Literal["A", "AAAA", "CNAME", "TXT", "MX"] = "A", record_name: str = "", content: str = "", ttl: int = 1, proxied: bool = False, priority: int | None = None, execute: bool = False) -> dict:
    """Preview or apply a DNS change. Update replaces supplied supported record fields; requires explicit record ID."""
    cf = client(profile)
    cf.check_zone(zone_id)
    path = f"zones/{zone_id}/dns_records"
    method = {"create": "POST", "update": "PATCH", "delete": "DELETE"}[operation]
    if operation != "create":
        path += "/" + cf_id(record_id or "")
    body = None
    if operation != "delete":
        if not record_name or not content or not (ttl == 1 or 60 <= ttl <= 86400):
            raise ValueError("Provide name, content and valid TTL (1=automatic or 60–86400)")
        if proxied and record_type not in ("A", "AAAA", "CNAME"):
            raise ValueError("This record type cannot be proxied")
        body = {"type": record_type, "name": record_name, "content": content, "ttl": ttl, "proxied": proxied}
        if record_type == "MX":
            if priority is None or not 0 <= priority <= 65535:
                raise ValueError("MX records require priority 0–65535")
            body["priority"] = priority
    if not execute:
        return {"preview": True, "profile": profile, "method": method, "path": path, "record": body}
    db = registry()
    db.audit("dns." + operation, profile + "/" + zone_id, "started")
    try:
        result = cf.request(method, path, **({"json": body} if body is not None else {}))
    except Exception:
        db.audit("dns." + operation, profile + "/" + zone_id, "failed_or_unknown")
        raise
    db.audit("dns." + operation, profile + "/" + zone_id, "success")
    return result


@mcp.tool(annotations=READ)
def cloudflare_tunnels(profile: str, page: int = 1) -> dict:
    """List Cloudflare tunnels in the profile account, without retrieving tunnel tokens."""
    if page < 1:
        raise ValueError("Page must be positive")
    cf = client(profile)
    return cf.request("GET", f'accounts/{cf.profile["account_id"]}/cfd_tunnel', params={"page": page, "per_page": 50, "is_deleted": "false"})


@mcp.tool(annotations=WRITE)
def cloudflare_tunnel_create(profile: str, tunnel_name: str, execute: bool = False) -> dict:
    """Preview or create a remotely managed tunnel. Connector installation and ingress are separate steps."""
    name(tunnel_name)
    cf = client(profile)
    body = {"name": tunnel_name, "config_src": "cloudflare"}
    if not execute:
        return {"preview": True, "profile": profile, "tunnel": body}
    db = registry()
    db.audit("tunnel.create", profile, "started")
    try:
        result = cf.request("POST", f'accounts/{cf.profile["account_id"]}/cfd_tunnel', json=body)["result"]
    except Exception:
        db.audit("tunnel.create", profile, "failed_or_unknown")
        raise
    db.audit("tunnel.create", profile, "success")
    return {key: result.get(key) for key in ("id", "name", "status", "created_at")}


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
