# linux-ssh-mcp

**An MCP server that gives any AI client full control over your Linux fleet through SSH — no agent installed on the remote hosts, just standard OpenSSH.**

Connect Claude Desktop, the Claude Code CLI, or any Model Context Protocol-compatible client to this container and manage your servers with plain English.

---

## What's inside

| Component | Detail |
|---|---|
| Base image | `python:3.12-slim` |
| MCP protocol | 2024-11-05 (stdio / JSON-RPC 2.0) |
| SSH library | `paramiko` |
| Tools exposed | **48** |
| Auth methods | SSH key, password, or SSH agent |
| Sudo support | Yes — via PTY + `sudo -S` |
| Multi-server | Yes — parallel broadcast included |

System packages pre-installed: `openssh-client`, `openssl`, `iputils-ping`, `traceroute`, `git`.  
No secrets are baked into the image — credentials are passed at runtime via `--env-file`.

---

## Quick start

```bash
# 1. Create your .env from the template
curl -fsSL https://raw.githubusercontent.com/lordraw/linux-mcp/main/.env.example -o .env
$EDITOR .env   # fill in your SSH servers

# 2. Run
docker run --rm -i --env-file .env lordraw/linux-mcp
```

The container speaks **stdio** — always keep `-i` so your MCP client can pipe JSON-RPC to it.

---

## Configuration

All configuration lives in a single `.env` file. Add one numbered block per server; the loader stops at the first missing `SERVER_N_HOST`.

```ini
# ── Server 1: root via SSH key ─────────────────────────────────────────────
SERVER_1_LABEL=web-server
SERVER_1_HOST=192.168.1.10
SERVER_1_PORT=22
SERVER_1_USER=root
SERVER_1_KEY_PATH=/root/.ssh/id_ed25519

# ── Server 2: unprivileged user + sudo ────────────────────────────────────
SERVER_2_LABEL=db-server
SERVER_2_HOST=192.168.1.20
SERVER_2_PORT=22
SERVER_2_USER=deploy
SERVER_2_KEY_PATH=/home/user/.ssh/deploy_rsa
SERVER_2_SUDO_PASSWORD=s3cr3t

# ── Server 3: password auth ───────────────────────────────────────────────
SERVER_3_LABEL=backup
SERVER_3_HOST=10.0.0.5
SERVER_3_PORT=2222
SERVER_3_USER=admin
SERVER_3_PASSWORD=mypassword
SERVER_3_SUDO_PASSWORD=mypassword
```

**Variable reference:**

| Variable | Required | Default | Description |
|---|---|---|---|
| `SERVER_N_HOST` | yes | — | Hostname or IP |
| `SERVER_N_PORT` | — | `22` | SSH port |
| `SERVER_N_USER` | — | `root` | Login username |
| `SERVER_N_LABEL` | — | `server-N` | Human-readable name |
| `SERVER_N_PASSWORD` | yes* | — | SSH password (*if no key) |
| `SERVER_N_KEY_PATH` | yes* | — | Path to SSH private key |
| `SERVER_N_SUDO_PASSWORD` | — | same as PASSWORD | Password for `sudo -S` |

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

Save as `docker-compose.yml` next to your `.env`:

```yaml
services:
  linux-mcp:
    image: lordraw/linux-mcp:latest
    stdin_open: true      # required for stdio MCP transport
    tty: false            # JSON-RPC is line-delimited, not a TTY
    env_file: .env
    volumes:
      - ${SSH_KEY_DIR:-~/.ssh}:/root/.ssh:ro
    restart: "no"         # MCP servers are spawned on-demand by the client
```

Run it:

```bash
docker compose run --rm linux-mcp
```

Or reference it from Claude Desktop / Claude Code as:

```json
{
  "mcpServers": {
    "linux-ssh": {
      "command": "docker",
      "args": ["compose", "-f", "/opt/linux-mcp/docker-compose.yml",
               "run", "--rm", "linux-mcp"]
    }
  }
}
```

---

## Connecting to MCP clients

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) /  
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "linux-ssh": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--env-file", "/opt/linux-mcp/.env",
        "-v", "/root/.ssh:/root/.ssh:ro",
        "lordraw/linux-mcp"
      ]
    }
  }
}
```

### Claude Code CLI

`.claude/settings.json` in your project root:

```json
{
  "mcpServers": {
    "linux-ssh": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--env-file", "/opt/linux-mcp/.env",
        "-v", "/root/.ssh:/root/.ssh:ro",
        "lordraw/linux-mcp"
      ]
    }
  }
}
```

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
