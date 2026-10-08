# Claude, Codex and ChatGPT

## Claude Code — ready for local use

After following the installation instructions in the README, register the server:

```bash
claude mcp add --scope user --transport stdio cloudflare-compose -- /absolute/path/to/cloudflare-compose/.venv/bin/python -m cloudflare_compose.server
```

Replace the example path with your checkout. Run `/mcp` in Claude Code to check its status. The process must receive your token environment variables; starting Claude from a terminal that already exports them is one option. For persistent credentials, use a private launcher or secret manager outside this repository. Never paste token values into a public config file or issue.

[Official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp)

## Claude Desktop — ready for local use

Open **Settings → Developer → Edit Config** and merge the following into `claude_desktop_config.json`, preserving other servers:

```json
{
  "mcpServers": {
    "cloudflare-compose": {
      "command": "/absolute/path/to/cloudflare-compose/.venv/bin/python",
      "args": ["-m", "cloudflare_compose.server"]
    }
  }
}
```

Supply token variables in the launched server environment. GUI applications may not inherit your terminal environment; a private launcher outside the repository can load your credentials before executing the command above. Fully quit and reopen Claude Desktop after changing the config. A copyable template is provided in `examples/claude-desktop.json`.

[Official Claude Desktop MCP documentation](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)

## Codex — ready for local use

```bash
codex mcp add cloudflare-compose -- /absolute/path/to/cloudflare-compose/.venv/bin/python -m cloudflare_compose.server
```

The repository also includes a local Codex plugin manifest and `.mcp.json`. Create the `.venv` within the plugin directory before using that package. A local plugin package is not a hosted ChatGPT connector.

## ChatGPT / claude.ai — remote deployment required

**This release is not a ready-to-connect ChatGPT URL.** It implements stdio only. ChatGPT custom connectors require a remote MCP endpoint; a local command or GitHub repository URL cannot be used as the endpoint.

To support ChatGPT, a deployment needs:

1. An MCP-compatible remote transport such as Streamable HTTP wrapping or extending this server.
2. HTTPS plus an OAuth-compatible authorization setup appropriate for the client, with access limited to the intended operator. Do not expose infrastructure tools anonymously.
3. Server-side Cloudflare credentials, SSH keys, host verification and persistent registry storage.
4. Client connection and end-to-end testing of authentication, tool discovery and authorization before calling the deployment ready.

These remote hosting and authentication components are **not bundled or tested in v0.1.0**. They are a separate deployment milestone. Publishing this source repository does not expose its author's infrastructure or create a hosted service.

[Official ChatGPT custom MCP documentation](https://developers.openai.com/api/docs/guides/custom-mcp-server)

## First prompts

After registering your own profile and machine:

- “List my Cloudflare zones using profile `work`.”
- “Check SSH and Docker Compose on machine `production`.”
- “Validate the `website` Compose project.”
- “Preview restarting the `web` service in project `website`.”

Project registration requires an existing remote Compose file. Multi-file Compose stacks, file upload and automatic infrastructure discovery are not supported by this release.
