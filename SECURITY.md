# Security and credential handling

This server is designed for a single trusted operator. Anyone allowed to invoke its tools has the authority of its configured Cloudflare tokens and SSH accounts. The registry is not a multi-user authorization boundary.

## Keep secrets outside the repository

- Supply Cloudflare tokens in the server process environment, preferably through a secret manager or an owner-readable launcher outside the checkout. Profiles store the **variable name**, not the token.
- Keep SSH private keys and verified `known_hosts` outside the checkout. Machine records hold file paths, not key contents.
- Keep the default state directory outside the checkout. The SQLite registry stores hostnames, account IDs, file paths and audit outcomes: treat that metadata as private too.
- Never commit local MCP client configuration containing credentials, environment files, database exports, private keys or command logs.
- `.gitignore` and `.dockerignore` exclude common credential and state filenames, but they cannot recognize every secret. Review staged changes before publishing.

## Execution boundaries

SSH host verification is mandatory; new host keys are never automatically trusted. Local SSH configuration files are disabled. The connector accepts a fixed set of Compose actions, shell-quotes arguments, and does not expose arbitrary shell execution. Docker access and trusted Compose files can still grant extensive control over the host.

Mutations default to preview. Setting `execute=true` applies them; this is a usability guard, not an authorization mechanism. MCP tool annotations are hints to clients, not enforcement of user approval. Do not connect untrusted clients or run unreviewed Compose files.

Application logs and DNS record contents can contain secrets. These tool results are **not automatically redacted**. Only request output you are comfortable sharing with your MCP client. Audit history omits operation output but is not tamper-proof.

The current transport is local stdio. There is no authenticated HTTP endpoint bundled in this release. Do not publish a raw unauthenticated bridge to this server. Remote integrations require an authenticated gateway with per-user authorization and an appropriate secret-management design.

## Optional nginx/certbot sudo policy

The operator may install a narrowly scoped rule on the registered machine. The exact paths must match `which` on that machine; this example is for user `adgsenpai`:

```sudoers
adgsenpai ALL=(root) NOPASSWD: /usr/bin/tee /etc/nginx/sites-available/*, /usr/bin/ln -sf /etc/nginx/sites-available/* /etc/nginx/sites-enabled/*, /usr/bin/rm -f /etc/nginx/sites-enabled/*, /usr/bin/rm -f /etc/nginx/sites-available/*, /usr/sbin/nginx -t, /usr/bin/systemctl reload nginx, /usr/bin/certbot --nginx *
```

The tools use `sudo -n`; they never prompt for or transmit a sudo password. Install this policy manually and verify it with `sudo -n -l`. Uploads are limited to `CF_COMPOSE_UPLOAD_MAX_BYTES` (200 MiB by default) and roots listed in `CF_COMPOSE_UPLOAD_ROOTS`. Upload exclusions are always applied, including when deletion is requested. Generated `.env` values are created on the server with OpenSSL and are never returned to the model or written to audit rows.

## Reporting vulnerabilities

Use GitHub's private vulnerability reporting feature in this repository when enabled. Do not put credentials, private endpoints or exploit details in public issues. If private reporting is unavailable, open a minimal issue asking for a private contact channel without sensitive details.
