from __future__ import annotations

import base64
import os
import re
import shlex
import httpx
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .core import Cloudflare, Registry, cf_id, compose_command, name, remote_path, run_ssh, upload_manifest, upload_ssh

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
def compose_run(project: str, action: Literal["status", "logs", "validate", "pull", "build", "up", "stop", "restart", "down"], service: str | None = None, execute: bool = False, tail: int = 100) -> dict:
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


def _audit_mutation(operation, target, execute, action):
    if not execute:
        return None
    db = registry(); db.audit(operation, target, "started")
    try:
        result = action()
    except Exception:
        db.audit(operation, target, "failed_or_unknown")
        raise
    db.audit(operation, target, result.get("outcome", "success") if isinstance(result, dict) else "success")
    return result


@mcp.tool(annotations=WRITE)
def project_sync(project: str, local_path: str, execute: bool = False, delete: bool = False) -> dict:
    """Preview or upload a directory/archive into a registered project, with fixed secret-bearing paths excluded."""
    target = registry().get("project", project)
    local = Path(local_path).expanduser().resolve()
    roots = [Path(x).expanduser().resolve() for x in os.environ.get("CF_COMPOSE_UPLOAD_ROOTS", "").split(":") if x]
    if not roots or not any(local == root or root in local.parents for root in roots):
        raise ValueError("local_path must be inside a CF_COMPOSE_UPLOAD_ROOTS root")
    cap = int(os.environ.get("CF_COMPOSE_UPLOAD_MAX_BYTES", str(200 * 1024 * 1024)))
    files, total = upload_manifest(local, cap)
    preview = {"preview": True, "file_count": len(files), "total_bytes": total, "target_dir": target["directory"], "excluded": sorted(upload_manifest.__globals__["EXCLUDED_UPLOAD_PARTS"]), "delete": delete}
    if not execute:
        return preview
    result = _audit_mutation("project.sync", project, True, lambda: upload_ssh(registry().get("machine", target["machine"]), local, target["directory"], delete))
    return {**preview, **result, "preview": False}


@mcp.tool(annotations=WRITE)
def project_env_init(project: str, keys: dict[str, str], generate: list[str], execute: bool = False) -> dict:
    """Create or extend a remote .env without returning generated values or storing them in the registry."""
    target = registry().get("project", project)
    if set(keys) & set(generate):
        raise ValueError("A key cannot be both supplied and generated")
    for key, value in keys.items():
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) or "\n" in value or "\r" in value:
            raise ValueError("keys must be uppercase environment names and values cannot contain newlines")
    for key in generate:
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise ValueError("generate contains an invalid environment key")
    requested = list(keys) + list(generate)
    if not execute:
        return {"preview": True, "keys": requested, "directory": target["directory"], "generated": generate}
    machine = registry().get("machine", target["machine"])
    assignments = [f"printf '%s\\n' {shlex.quote(k + '=' + v)}" for k, v in keys.items()]
    assignments += [f"printf '%s=' {shlex.quote(k)}; openssl rand -hex 24" for k in generate]
    script = " && ".join(assignments) or ":"
    command = f"mkdir -p {shlex.quote(target['directory'])} && touch {shlex.quote(target['directory']+'/.env')} && chmod 600 {shlex.quote(target['directory']+'/.env')} && ({{ grep -E '^[A-Z_][A-Z0-9_]*=' {shlex.quote(target['directory']+'/.env')} || true; }} )"
    # Use an awk-backed fixed key filter and append only missing requested names.
    append = " && ".join(f"grep -q '^\\s*{k}=' {shlex.quote(target['directory']+'/.env')} || ( {assign} >> {shlex.quote(target['directory']+'/.env')} )" for k, assign in zip(requested, assignments))
    command = f"if [ -f {shlex.quote(target['directory']+'/.env')} ]; then {append or ':'}; else {script} > {shlex.quote(target['directory']+'/.env')}; fi && chmod 600 {shlex.quote(target['directory']+'/.env')}"
    result = _audit_mutation("project.env_init", project, True, lambda: run_ssh(machine, command))
    return {"preview": False, "keys": requested, **result}


def _hostname(value: str) -> str:
    if len(value) > 253 or not re.fullmatch(r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", value):
        raise ValueError("Invalid DNS hostname")
    return value.lower()


def _nginx_config(host, port, body):
    return f"server {{\n    listen 80;\n    listen [::]:80;\n    server_name {host};\n    client_max_body_size {body}m;\n    location / {{\n        proxy_pass http://127.0.0.1:{port};\n        proxy_http_version 1.1;\n        proxy_set_header Host $host;\n        proxy_set_header X-Forwarded-Host $host;\n        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n        proxy_set_header X-Forwarded-Proto $scheme;\n    }}\n}}\n"


def _path_env(key, default):
    value = os.environ.get(key, default)
    if not value.startswith("/") or any(c in value for c in "\n\r;|&"):
        raise ValueError(f"{key} must be an absolute binary path")
    return value


@mcp.tool(annotations=WRITE)
def nginx_site(machine: str, hostname: str, upstream_port: int, max_body_mb: int = 5, execute: bool = False, replace: bool = False) -> dict:
    """Preview or install a fixed nginx reverse proxy site, testing before reload and rolling back failed new sites."""
    host = _hostname(hostname)
    if not 1024 <= upstream_port <= 65535 or not 1 <= max_body_mb <= 100:
        raise ValueError("upstream_port must be 1024-65535 and max_body_mb must be 1-100")
    config = _nginx_config(host, upstream_port, max_body_mb)
    result = {"preview": True, "hostname": host, "config": config}
    if not execute:
        return result
    machine_data = registry().get("machine", machine)
    path = f"/etc/nginx/sites-available/{host}"; link = f"/etc/nginx/sites-enabled/{host}"
    if not replace and run_ssh(machine_data, f"test ! -e {shlex.quote(path)}")["exit_code"] != 0:
        raise ValueError("nginx site already exists; set replace=true to overwrite")
    encoded = base64.b64encode(config.encode()).decode("ascii")
    sudo = _path_env("CF_COMPOSE_SUDO", "/usr/bin/sudo")
    tee = _path_env("CF_COMPOSE_TEE", "/usr/bin/tee")
    ln = _path_env("CF_COMPOSE_LN", "/usr/bin/ln")
    nginx = _path_env("CF_COMPOSE_NGINX", "/usr/sbin/nginx")
    systemctl = _path_env("CF_COMPOSE_SYSTEMCTL", "/usr/bin/systemctl")
    write = f"printf %s {shlex.quote(encoded)} | /usr/bin/base64 -d | {sudo} -n {tee} {shlex.quote(path)} >/dev/null && {sudo} -n {ln} -sf {shlex.quote(path)} {shlex.quote(link)} && {sudo} -n {nginx} -t"
    tested = run_ssh(machine_data, write)
    if tested["exit_code"] != 0:
        run_ssh(machine_data, f"{sudo} -n /usr/bin/rm -f {shlex.quote(link)} {shlex.quote(path)}")
        return {**result, "preview": False, "outcome": "failed", "nginx_test": tested}
    reloaded = run_ssh(machine_data, f"{sudo} -n {systemctl} reload nginx")
    return {**result, "preview": False, "outcome": reloaded["outcome"], "nginx_test": tested, "reload": reloaded}


@mcp.tool(annotations=WRITE)
def certbot_issue(machine: str, hostname: str, email: str, execute: bool = False) -> dict:
    """Preview or issue a Let's Encrypt certificate through certbot's nginx plugin."""
    host = _hostname(hostname)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Invalid email address")
    sudo = _path_env("CF_COMPOSE_SUDO", "/usr/bin/sudo")
    certbot = _path_env("CF_COMPOSE_CERTBOT", "/usr/bin/certbot")
    command = f"{sudo} -n {certbot} --nginx -d {shlex.quote(host)} --redirect --non-interactive --agree-tos -m {shlex.quote(email)}"
    if not execute:
        return {"preview": True, "hostname": host, "command": command}
    result = _audit_mutation("certbot.issue", machine + "/" + host, True, lambda: run_ssh(registry().get("machine", machine), command))
    return {"preview": False, **result}


@mcp.tool(annotations=READ)
def http_check(url: str) -> dict:
    """GET a URL and return only its status code and final URL."""
    if not re.fullmatch(r"https?://[^\s]+", url):
        raise ValueError("URL must use http or https")
    try:
        response = httpx.get(url, timeout=10, follow_redirects=True)
    except httpx.RequestError:
        raise ValueError("HTTP check failed or timed out") from None
    return {"status_code": response.status_code, "final_url": str(response.url)}


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
