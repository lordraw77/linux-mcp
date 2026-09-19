# linux-ssh-mcp

**An MCP server that gives any AI client full control over your Linux fleet through SSH — no agent installed on the remote hosts, just standard OpenSSH.**

Connect Claude Desktop, the Claude Code CLI, Cursor, Cline, or any Model Context Protocol-compatible client to this container and manage your servers with plain English.

---

## What's inside

| Component | Detail |
|---|---|
| Base image | `python:3.12-slim` |
| MCP protocol | 2024-11-05 |
| Transports | **stdio** (JSON-RPC 2.0) · **Streamable HTTP** · SSE (legacy, deprecated) |
| SSH library | `paramiko` |
| Tools exposed | **48** |
| Auth methods | SSH key, password, or SSH agent |
| Sudo support | Yes — via PTY + `sudo -S` |
| Multi-server | Yes — parallel broadcast included |

System packages pre-installed: `openssh-client`, `openssl`, `iputils-ping`, `traceroute`, `git`.  
No secrets are baked into the image — credentials are passed at runtime via `--env-file`.

---

## Transport modes

| Mode | When to use | How clients connect |
|---|---|---|
| `stdio` (default) | Claude Desktop, Claude Code CLI — client spawns the container as a child process | JSON-RPC 2.0 over stdin/stdout |
| `streamable-http` | Cursor, Cline, Continue, or any HTTP-capable MCP client — container runs as a persistent service | `http://<host>:<port>/mcp` |
| `sse` *(deprecated)* | Legacy HTTP+SSE clients only — kept for backward compatibility | `http://<host>:<port>/sse` |

---

## Quick start

### stdio mode (Claude Desktop / Claude Code)

```bash
# 1. Create your .env from the template
curl -fsSL https://raw.githubusercontent.com/lordraw77/linux-mcp/main/.env.example -o .env
$EDITOR .env   # fill in your SSH servers

# 2. Run (keep -i so the MCP client can pipe JSON-RPC to stdin)
docker run --rm -i --env-file .env lordraw/linux-mcp
```

### Streamable HTTP mode (Cursor / Cline / HTTP clients)

```bash
# 1. Create your .env from the template (set UXMCP_TRANSPORT=streamable-http)
curl -fsSL https://raw.githubusercontent.com/lordraw77/linux-mcp/main/.env.example -o .env
$EDITOR .env

# 2. Run as a persistent HTTP service
docker run --rm -p 9880:9880 \
  --env-file .env \
  -e UXMCP_TRANSPORT=streamable-http \
  -e UXMCP_HTTP_PORT=9880 \
  lordraw/linux-mcp

# Connect your MCP client to:  http://localhost:9880/mcp
```

> The legacy `UXMCP_TRANSPORT=sse` mode (endpoint `/sse`) is deprecated but still supported.

---

## Configuration

All configuration lives in a single `.env` file.

### Transport

```ini
# stdio (default) — client spawns the container via stdin/stdout
UXMCP_TRANSPORT=stdio

# streamable-http — container runs as a persistent HTTP service (endpoint /mcp)
# UXMCP_TRANSPORT=streamable-http
# UXMCP_HTTP_HOST=0.0.0.0   # bind address (default: 0.0.0.0)
# UXMCP_HTTP_PORT=8080      # TCP port     (default: 8080)

# sse — legacy, deprecated (endpoint /sse); UXMCP_SSE_HOST/PORT still work as fallbacks
```

### SSH servers

Add one numbered block per server; the loader stops at the first missing `UXMCP_SERVER_N_HOST`.

```ini
# ── Server 1: root via SSH key ─────────────────────────────────────────────
UXMCP_SERVER_1_LABEL=web-server
UXMCP_SERVER_1_HOST=192.168.1.10
UXMCP_SERVER_1_PORT=22
UXMCP_SERVER_1_USER=root
UXMCP_SERVER_1_KEY_PATH=/root/.ssh/id_ed25519

# ── Server 2: unprivileged user + sudo ────────────────────────────────────
UXMCP_SERVER_2_LABEL=db-server
UXMCP_SERVER_2_HOST=192.168.1.20
UXMCP_SERVER_2_PORT=22
UXMCP_SERVER_2_USER=deploy
UXMCP_SERVER_2_KEY_PATH=/home/user/.ssh/deploy_rsa
UXMCP_SERVER_2_SUDO_PASSWORD=s3cr3t

# ── Server 3: password auth ───────────────────────────────────────────────
UXMCP_SERVER_3_LABEL=backup
UXMCP_SERVER_3_HOST=10.0.0.5
UXMCP_SERVER_3_PORT=2222
UXMCP_SERVER_3_USER=admin
UXMCP_SERVER_3_PASSWORD=mypassword
UXMCP_SERVER_3_SUDO_PASSWORD=mypassword
```

**Variable reference:**

| Variable | Required | Default | Description |
|---|---|---|---|
| `UXMCP_TRANSPORT` | — | `stdio` | Transport mode: `stdio`, `streamable-http` or `sse` (deprecated) |
| `UXMCP_HTTP_HOST` | — | `0.0.0.0` | Bind address (HTTP modes only; legacy alias `UXMCP_SSE_HOST`) |
| `UXMCP_HTTP_PORT` | — | `8080` | TCP port (HTTP modes only; legacy alias `UXMCP_SSE_PORT`) |
| `UXMCP_SERVER_N_HOST` | yes | — | Hostname or IP |
| `UXMCP_SERVER_N_PORT` | — | `22` | SSH port |
| `UXMCP_SERVER_N_USER` | — | `root` | Login username |
| `UXMCP_SERVER_N_LABEL` | — | `server-N` | Human-readable name |
| `UXMCP_SERVER_N_PASSWORD` | yes* | — | SSH password (*if no key) |
| `UXMCP_SERVER_N_KEY_PATH` | yes* | — | Path to SSH private key |
| `UXMCP_SERVER_N_SUDO_PASSWORD` | — | same as PASSWORD | Password for `sudo -S` |

> If both `KEY_PATH` and `PASSWORD` are set, the key takes precedence.  
> If `USER=root`, `use_sudo` is silently ignored for all tools.

### Mounting SSH keys

When using key-based auth, mount the key directory into the container so the path resolves:

```bash
docker run --rm -i \
  --env-file .env \
  -v $HOME/.ssh:/root/.ssh:ro \
  lordraw/linux-mcp
```

---

## Docker Compose

The bundled `docker-compose.yml` provides two services selectable via profiles.

### stdio (default)

```bash
docker compose run --rm linux-mcp
```

### Streamable HTTP (persistent HTTP service)

```bash
# Start in background
docker compose --profile http up -d linux-mcp-http

# View logs
docker compose logs -f linux-mcp-http

# Stop
docker compose --profile http down
```

The service listens on `UXMCP_HTTP_PORT` (default `9880` in compose, or override in `.env`).

> Legacy: `docker compose --profile sse up -d linux-mcp-sse` (deprecated SSE, endpoint `/sse`) is still available.

---

## Connecting to MCP clients

### Claude Desktop — stdio

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) /  
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "linux-ssh": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--env-file", "/path/to/.env",
        "-v", "/home/user/.ssh:/root/.ssh:ro",
        "lordraw/linux-mcp"
      ]
    }
  }
}
```

### Claude Code CLI — stdio

`.claude/settings.json` in your project root:

```json
{
  "mcpServers": {
    "linux-ssh": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--env-file", "/path/to/.env",
        "-v", "/home/user/.ssh:/root/.ssh:ro",
        "lordraw/linux-mcp"
      ]
    }
  }
}
```

### Cursor / Cline / Continue — Streamable HTTP

Start the HTTP service first (see above), then point the client at:

```
http://localhost:8080/mcp
```

Cursor (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "linux-ssh": {
      "type": "http",
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

Legacy SSE clients: `"transport": "sse"`, `"url": "http://localhost:8080/sse"` (deprecated).

---

## What you can ask

Once connected, talk to your servers in plain English. Examples:

### Discovery & overview
```
Which servers are configured?
Show system info for all servers.
```

### Commands
```
Run `uptime` on web-server.
Execute `df -h` on all servers simultaneously.
Run `systemctl status nginx` on web-server with sudo.
```

### Files
```
Read /etc/nginx/nginx.conf from web-server.
Tail the last 100 lines of /var/log/syslog on db-server.
Create directory /opt/myapp/releases on web-server.
Download /etc/hosts from db-server.
```

### Monitoring
```
Collect 5 CPU and memory samples from web-server, 3 seconds apart.
Show the top 10 processes by memory on db-server.
```

### Services & processes
```
Restart the nginx service on web-server.
List all failing systemd units on db-server.
Kill process 1234 on web-server with sudo.
```

### Docker
```
List running containers on web-server.
Show the last 200 log lines for the api container.
Restart the worker container on db-server.
```

### Packages
```
Install htop on web-server.
List installed packages matching python on db-server.
```

### Firewall
```
Show firewall rules on web-server.
Open port 8080/tcp on web-server.
Block port 3306/tcp on db-server.
```

### Network
```
Ping 8.8.8.8 from web-server.
Traceroute to github.com from web-server.
Check if port 5432 is reachable on db-server.
```

### Users & groups
```
List all non-system users on web-server.
Create user deploy with bash shell on web-server.
Show all groups on db-server.
```

### Disk & storage
```
Show disk usage for /var on db-server.
List all mounted filesystems on web-server.
```

### Cron
```
List all crontabs on web-server.
Add cron job "0 2 * * * /opt/backup.sh" on web-server.
Remove cron jobs matching backup from web-server.
```

### SSL / TLS
```
Check the TLS certificate for example.com:443 via web-server.
Is the certificate for api.mycompany.com expiring soon?
```

### Git
```
Show git status of /opt/myapp on web-server.
Pull the latest changes for /opt/myapp on web-server, branch main.
Show the last 5 commits for /opt/myapp on db-server.
```

### Web servers
```
Reload nginx on web-server.
Test the Apache config on db-server.
Show nginx status on web-server.
```

### Multi-server broadcast
```
Run free -m on web-server and db-server at the same time.
Deploy /opt/deploy.sh across all servers in parallel.
```

---

## Tags

| Tag | Description |
|---|---|
| `latest` | Most recent build from `main` |
| `vX.Y.Z` | Specific release |
| `vX.Y.Z-N-gSHA` | Pre-release / between tags |

---

## Source

GitHub: [lordraw77/linux-mcp](https://github.com/lordraw77/linux-mcp)
