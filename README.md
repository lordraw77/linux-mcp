# linux-ssh-mcp

A **Model Context Protocol (MCP) server** that exposes **48 SSH-based tools** for
managing remote Linux servers. Connect any MCP-compatible client (Claude Desktop,
`claude-code` CLI, or `agent.py`) and control your fleet through natural language.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Configuration](#configuration)
   - [SSH Servers](#ssh-servers)
5. [Running the MCP Server](#running-the-mcp-server)
6. [Tool Reference](#tool-reference)
   - [Core](#core)
   - [File Operations](#file-operations)
   - [File Search](#file-search)
   - [System Information](#system-information)
   - [Monitoring](#monitoring)
   - [Process Management](#process-management)
   - [Service Management (systemd)](#service-management-systemd)
   - [Log Management](#log-management)
   - [Docker](#docker)
   - [Package Management](#package-management)
   - [Firewall](#firewall)
   - [Network Tools](#network-tools)
   - [User Management](#user-management)
   - [Disk and Storage](#disk-and-storage)
   - [Cron](#cron)
   - [SSL / TLS](#ssl--tls)
   - [Git](#git)
   - [Web Servers](#web-servers)
   - [Multi-Server](#multi-server)
7. [Authentication](#authentication)
8. [Security Considerations](#security-considerations)

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     MCP Client / Agent                  │
│  (Claude Desktop, claude-code CLI, or agent.py)         │
└───────────────────────┬─────────────────────────────────┘
                        │  stdio  JSON-RPC 2.0
                        ▼
┌─────────────────────────────────────────────────────────┐
│                     server.py                           │
│  MCP Server  ·  Protocol: MCP 2024-11-05                │
│  48 tools registered via @app.list_tools()              │
└───────────────────────┬─────────────────────────────────┘
                        │  Python function calls
                        ▼
┌─────────────────────────────────────────────────────────┐
│                  ssh_manager.py                         │
│  paramiko-based SSH/SFTP connection manager             │
│  Credentials loaded from .env via python-dotenv         │
└───────────────────────┬─────────────────────────────────┘
                        │  SSH / SFTP
                        ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  Server 1    │  │  Server 2    │  │  Server N    │  ...
│  (root/key)  │  │  (user+sudo) │  │  (any auth)  │
└──────────────┘  └──────────────┘  └──────────────┘
```

**Files:**

| File | Role |
|---|---|
| `server.py` | MCP server entry-point; registers and dispatches all 48 tools |
| `ssh_manager.py` | SSH/SFTP connection manager; implements all tool logic |
| `agent.py` | Interactive CLI agent; supports 7 AI providers |
| `.env` | Runtime secrets (not committed) |
| `.env.example` | Template for all configuration variables |
| `requirements.txt` | Python dependencies |

---

## Requirements

- Python **3.10+** (3.12 recommended — the `mcp` library requires ≥ 3.10)
- Remote servers running **OpenSSH** with SFTP subsystem enabled

---

## Installation

```bash
git clone <repo-url> /opt/linux-mcp
cd /opt/linux-mcp

# Install dependencies (use Python 3.12 if 3.9 is the system default)
python3.12 -m pip install -r requirements.txt

# Copy and edit the configuration
cp .env.example .env
$EDITOR .env
```

---

## Configuration

All configuration is stored in `.env`. Never commit this file — it is listed in
`.gitignore`.

### SSH Servers

Servers are configured with a numbered prefix `SERVER_N_*`. The loader scans
`N = 1, 2, 3, …` and stops at the first `N` where `SERVER_N_HOST` is missing,
so you can add as many servers as needed.

```ini
# ── Server 1: root access via SSH key ─────────────────────────────────────────
SERVER_1_LABEL=web-server
SERVER_1_HOST=192.168.1.10
SERVER_1_PORT=22
SERVER_1_USER=root
SERVER_1_KEY_PATH=/root/.ssh/id_ed25519
# SERVER_1_PASSWORD=               # alternative to KEY_PATH

# ── Server 2: unprivileged user + sudo ────────────────────────────────────────
SERVER_2_LABEL=db-server
SERVER_2_HOST=192.168.1.20
SERVER_2_PORT=22
SERVER_2_USER=deploy
SERVER_2_KEY_PATH=/home/user/.ssh/deploy_rsa
SERVER_2_SUDO_PASSWORD=s3cr3t       # used when use_sudo=true

# ── Server 3: password auth ───────────────────────────────────────────────────
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
| `SERVER_N_HOST` | ✅ | — | Hostname or IP of the SSH server |
| `SERVER_N_PORT` | — | `22` | SSH port |
| `SERVER_N_USER` | — | `root` | SSH login username |
| `SERVER_N_LABEL` | — | `server-N` | Human-readable name (shown in listings) |
| `SERVER_N_PASSWORD` | ✅* | — | SSH password (*required if no KEY_PATH) |
| `SERVER_N_KEY_PATH` | ✅* | — | Absolute path to SSH private key |
| `SERVER_N_SUDO_PASSWORD` | — | same as PASSWORD | Password used for `sudo -S` |

> If both `KEY_PATH` and `PASSWORD` are set, the key takes precedence.
> If the user is `root`, `use_sudo` is silently ignored.

---

## Running the MCP Server

The server communicates over **stdio** using JSON-RPC 2.0 as required by the
MCP protocol. It is not invoked directly but launched by an MCP client.

### With Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "linux-ssh": {
      "command": "python3.12",
      "args": ["/opt/linux-mcp/server.py"]
    }
  }
}
```

### With Claude Code CLI

Add to `.claude/settings.json` in your project root:

```json
{
  "mcpServers": {
    "linux-ssh": {
      "command": "python3.12",
      "args": ["/opt/linux-mcp/server.py"]
    }
  }
}
```

### Manual test

```bash
# Verify the server starts and lists tools
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | python3.12 server.py
```

---

## Tool Reference

All tools accept `server_id` (string) as their first required parameter, except
`list_servers` (no parameters) and `broadcast_command` (takes `server_ids` array).

---

### Core

#### `list_servers`

Returns the list of all configured servers. No credentials are included.

**Parameters:** none

**Returns:**
```json
[
  {
    "id": "1",
    "label": "web-server",
    "host": "192.168.1.10",
    "port": 22,
    "user": "root",
    "auth": "key",
    "is_root": true
  }
]
```

---

#### `execute_command`

Executes an arbitrary shell command on the remote server.

**Parameters:**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | Target server ID |
| `command` | string | ✅ | — | Shell command to execute |
| `use_sudo` | boolean | — | `false` | Wrap with `sudo -S` (ignored for root) |
| `timeout` | integer | — | `60` | Execution timeout in seconds |

**Returns:**
```json
{ "stdout": "...", "stderr": "...", "exit_code": 0, "ok": true }
```

**Notes:**
- When `use_sudo=true` and the user is not root, a PTY channel is opened and
  `SERVER_N_SUDO_PASSWORD` (or `SERVER_N_PASSWORD`) is written to stdin.

---

### File Operations

#### `read_file`

Reads a remote file via SFTP and returns its content as UTF-8 text.

| Name | Type | Required | Description |
|---|---|---|---|
| `server_id` | string | ✅ | |
| `path` | string | ✅ | Absolute remote path |

**Returns:** file content as a plain string.

---

#### `write_file`

Writes (or overwrites) a remote file via SFTP. Creates the file if it does not
exist. The parent directory must already exist.

| Name | Type | Required | Description |
|---|---|---|---|
| `server_id` | string | ✅ | |
| `path` | string | ✅ | Absolute remote path |
| `content` | string | ✅ | UTF-8 content to write |

**Returns:** `{ "ok": true, "path": "/etc/myapp/config.yaml" }`

---

#### `get_file_stat`

Returns detailed metadata for a remote path (size, permissions, UID/GID, timestamps).

| Name | Type | Required |
|---|---|---|
| `server_id` | string | ✅ |
| `path` | string | ✅ |

**Returns:**
```json
{
  "path": "/var/log/nginx/access.log",
  "size": 1048576,
  "uid": 33, "gid": 33,
  "permissions": "0o644",
  "is_dir": false,
  "atime": 1716400000.0,
  "mtime": 1716400000.0,
  "stat_output": "-rw-r--r-- 1 www-data www-data ..."
}
```

---

#### `delete_path`

Deletes a file or directory on the remote server.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `path` | string | ✅ | — | Absolute path to delete |
| `recursive` | boolean | — | `false` | Required for non-empty directories |
| `use_sudo` | boolean | — | `false` | |

**Safety guard:** The following paths are **always refused**:
`/`, `/etc`, `/bin`, `/sbin`, `/usr`, `/lib`, `/lib64`, `/boot`, `/dev`, `/proc`, `/sys`.

---

#### `create_directory`

Creates a directory and all intermediate parent directories (`mkdir -p`).

| Name | Type | Required | Default |
|---|---|---|---|
| `server_id` | string | ✅ | — |
| `path` | string | ✅ | — |
| `use_sudo` | boolean | — | `false` |

---

#### `upload_file`

Uploads a local file to the remote server via SFTP.

> `local_path` is resolved on the machine where `server.py` runs.

| Name | Type | Required | Description |
|---|---|---|---|
| `server_id` | string | ✅ | |
| `local_path` | string | ✅ | Absolute local path |
| `remote_path` | string | ✅ | Absolute remote destination path |

**Returns:** `{ "local_path": "...", "remote_path": "...", "size": 10485760 }`

---

#### `download_file`

Downloads a remote file to the local filesystem via SFTP. If `local_path` is
a directory, the original filename is preserved inside it.

| Name | Type | Required | Description |
|---|---|---|---|
| `server_id` | string | ✅ | |
| `remote_path` | string | ✅ | |
| `local_path` | string | ✅ | Absolute local destination path or directory |

---

#### `list_directory`

Lists entries in a remote directory via SFTP.

| Name | Type | Required | Default |
|---|---|---|---|
| `server_id` | string | ✅ | — |
| `path` | string | — | `/` |

**Returns:**
```json
[
  { "name": "conf.d", "type": "directory", "size": 4096, "permissions": "0o755" },
  { "name": "nginx.conf", "type": "file", "size": 2867, "permissions": "0o644" }
]
```

Directories appear first, then files, both sorted alphabetically.

---

### File Search

#### `search_files`

Searches for files under a remote directory using `find`.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `path` | string | ✅ | — | Base directory |
| `name_pattern` | string | — | `*` | Shell glob (e.g. `*.log`) |
| `file_type` | string | — | `any` | `file`, `directory`, or `any` |
| `modified_within_days` | integer | — | `0` | If > 0, only files modified in last N days |
| `max_results` | integer | — | `100` | Capped at 500 |

**Returns:** list of absolute remote paths.

---

#### `grep_files`

Recursively searches for a text pattern inside files.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `path` | string | ✅ | — | Base directory |
| `pattern` | string | ✅ | — | Regex or literal string |
| `file_glob` | string | — | `*` | File name filter (e.g. `*.conf`) |
| `case_insensitive` | boolean | — | `false` | |
| `max_results` | integer | — | `100` | Capped at 500 |

**Returns:** `[{ "file": "...", "line": "42", "match": "..." }]`

---

### System Information

#### `get_system_info`

Collects hostname, uname, uptime, CPU count, memory (`free -h`), disk (`df -h`), and OS release.

**Parameters:** `server_id`

**Returns:**
```json
{
  "hostname": "web01.example.com",
  "uname": "Linux web01 5.15.0-97-generic ...",
  "uptime": "up 42 days, 3 hours",
  "cpu_cores": "8",
  "memory": "total used free ...",
  "disk": "Filesystem Size Used Avail Use% Mounted on ...",
  "os_release": "NAME=\"Ubuntu\"\nVERSION=\"22.04.3 LTS\""
}
```

---

#### `get_network_info`

Returns IP addresses (`ip addr`), listening ports (`ss -tlnp`), routing table, and DNS resolvers.

**Parameters:** `server_id`

---

#### `check_port`

Checks whether a TCP port is reachable from the remote server (uses `nc` or `bash /dev/tcp`).

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `host` | string | ✅ | — | Target hostname or IP; use `localhost` for local services |
| `port` | integer | ✅ | — | |
| `timeout` | integer | — | `5` | Seconds |

**Returns:** `{ "host": "localhost", "port": 5432, "state": "open" }`

---

### Monitoring

#### `watch_metrics`

Collects CPU load averages, memory usage, and root disk usage at regular intervals.
Useful for diagnosing transient load spikes.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `samples` | integer | — | `3` | Number of data points to collect (1-10) |
| `interval` | integer | — | `2` | Seconds between samples (1-30) |

**Returns:**
```json
[
  {
    "timestamp": 1716400000,
    "load_1m": "0.45", "load_5m": "0.38", "load_15m": "0.30",
    "mem_total_mb": "7965", "mem_used_mb": "4120", "mem_free_mb": "512",
    "disk_total": "50G", "disk_used": "21G", "disk_avail": "27G", "disk_pct": "44%"
  }
]
```

---

#### `top_processes`

Returns the top-N processes sorted by CPU or memory usage.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `count` | integer | — | `10` | Number of processes to return (1-100) |
| `sort_by` | string | — | `cpu` | `cpu` or `mem` |

**Returns:**
```json
[
  { "user": "www-data", "pid": "1234", "cpu_pct": "12.5", "mem_pct": "2.1",
    "command": "nginx: worker process" }
]
```

---

### Process Management

#### `list_processes`

Lists running processes using `ps aux`, optionally filtered by a pattern matched
against the full command line.

| Name | Type | Required | Default |
|---|---|---|---|
| `server_id` | string | ✅ | — |
| `pattern` | string | — | `""` |

**Returns:**
```json
[
  { "user": "root", "pid": "1", "cpu": "0.0", "mem": "0.1",
    "vsz": "165468", "rss": "11248", "stat": "Ss", "command": "systemd" }
]
```

---

#### `kill_process`

Sends a signal to a process by PID or name pattern.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `target` | string | ✅ | — | PID (numeric) or command pattern for `pkill -f` |
| `signal` | string | — | `TERM` | `TERM`, `KILL`, `HUP`, `INT`, `USR1`, `USR2`, `STOP`, `CONT` |
| `use_sudo` | boolean | — | `false` | |

---

### Service Management (systemd)

#### `service_control`

Controls a systemd service via `systemctl`.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `service` | string | ✅ | — | Service name (e.g. `nginx`, `postgresql`) |
| `action` | string | ✅ | — | `start`, `stop`, `restart`, `reload`, `enable`, `disable`, `status`, `is-active`, `is-enabled`, `mask`, `unmask` |
| `use_sudo` | boolean | — | `true` | Auto-disabled for root |

**Returns:** `CommandResult` dict (`stdout`, `stderr`, `exit_code`, `ok`).

---

#### `list_services`

Lists systemd service units filtered by state.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `state` | string | — | `all` | `all`, `running`, `failed`, `inactive` |

**Returns:**
```json
[
  { "unit": "nginx.service", "load": "loaded", "active": "active",
    "sub": "running", "description": "A high performance web server" }
]
```

---

### Log Management

#### `tail_log`

Returns the last N lines of a log file or systemd journal. Provide **either**
`path` or `unit`.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `path` | string | — | `""` | Absolute log file path |
| `unit` | string | — | `""` | Systemd service name for `journalctl -u` |
| `lines` | integer | — | `50` | Max 5000 |

**Returns:** plain text string.

---

#### `grep_logs`

Searches for a pattern in a log file and returns matching lines.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `path` | string | ✅ | — | Absolute log file path |
| `pattern` | string | ✅ | — | Search string or regex |
| `case_insensitive` | boolean | — | `false` | |
| `max_lines` | integer | — | `200` | Max 2000 |

---

### Docker

#### `docker_ps`

Lists Docker containers.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `all_containers` | boolean | — | `false` | `true` includes stopped containers |

**Returns:**
```json
[
  { "id": "a1b2c3d4", "name": "nginx-proxy", "image": "nginx:latest",
    "status": "Up 3 days", "ports": "0.0.0.0:80->80/tcp", "created": "..." }
]
```

---

#### `docker_logs`

Returns the last N log lines from a container (stdout + stderr merged).

| Name | Type | Required | Default |
|---|---|---|---|
| `server_id` | string | ✅ | — |
| `container` | string | ✅ | — |
| `lines` | integer | — | `50` |
| `use_sudo` | boolean | — | `false` |

---

#### `docker_control`

Controls a Docker container.

| Name | Type | Required | Description |
|---|---|---|---|
| `server_id` | string | ✅ | |
| `container` | string | ✅ | Container name or ID |
| `action` | string | ✅ | `start`, `stop`, `restart`, `pause`, `unpause`, `kill`, `rm` |
| `use_sudo` | boolean | — | default `false` |

---

### Package Management

#### `list_packages`

Lists installed packages. Package manager is auto-detected:
`dnf` → `yum` → `apt-get` → `apk` → `zypper` → `pacman`.

| Name | Type | Required | Default |
|---|---|---|---|
| `server_id` | string | ✅ | — |
| `pattern` | string | — | `""` |

**Returns:** `[{ "name": "nginx", "version": "1.24.0-1.el9" }]` (sorted by name).

---

#### `package_control`

Installs, removes, or updates packages using the auto-detected package manager.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `package` | string | — | `""` | Leave empty for `action=upgrade` |
| `action` | string | ✅ | — | `install`, `remove`, `update`, `upgrade` |
| `use_sudo` | boolean | — | `true` | |

> **Warning:** `upgrade` updates all packages on the system.

---

### Firewall

#### `firewall_rules`

Returns current firewall rules from whichever managers are available:
`ufw`, `firewalld`, and `iptables`.

**Parameters:** `server_id`

**Returns:**
```json
{
  "ufw": "Status: active\n\nTo                   Action ...",
  "iptables": "Chain INPUT (policy ACCEPT)\ntarget   prot ..."
}
```

---

#### `firewall_control`

Opens or closes a TCP/UDP port using the first available firewall manager
(`ufw` → `firewall-cmd` → `iptables`).

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `action` | string | ✅ | — | `allow` or `deny` |
| `port` | integer | ✅ | — | 1-65535 |
| `protocol` | string | — | `tcp` | `tcp` or `udp` |
| `use_sudo` | boolean | — | `true` | |

**Returns:** `CommandResult` dict.

**Notes:**
- For `ufw`: runs `ufw allow|deny PORT/PROTO`.
- For `firewalld`: runs `firewall-cmd --permanent` then `--reload`.
- For `iptables`: adds/removes an `INPUT` chain rule.

---

### Network Tools

#### `ping_host`

Runs `ping` from the remote server to a target host.

| Name | Type | Required | Default |
|---|---|---|---|
| `server_id` | string | ✅ | — |
| `host` | string | ✅ | — |
| `count` | integer | — | `4` |

**Returns:**
```json
{ "host": "8.8.8.8", "output": "PING 8.8.8.8 ...\n64 bytes from ...", "ok": true }
```

---

#### `traceroute_host`

Runs `traceroute` (or `tracepath` as fallback) from the remote server to a host.

| Name | Type | Required | Default |
|---|---|---|---|
| `server_id` | string | ✅ | — |
| `host` | string | ✅ | — |
| `max_hops` | integer | — | `20` |

**Returns:** plain text traceroute output.

---

### User Management

#### `list_users`

Returns all local user accounts from `/etc/passwd`.

**Parameters:** `server_id`

**Returns:**
```json
[
  { "username": "deploy", "uid": "1001", "gid": "1001",
    "comment": "Deploy User", "home": "/home/deploy", "shell": "/bin/bash" }
]
```

---

#### `user_control`

Manages a local user account.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `username` | string | ✅ | — | |
| `action` | string | ✅ | — | `add`, `remove`, `lock`, `unlock`, `passwd` |
| `password` | string | — | `""` | Required for `add` (optional) and `passwd` |
| `shell` | string | — | `""` | Login shell for `add` (e.g. `/bin/bash`) |
| `use_sudo` | boolean | — | `true` | |

**Actions:**
- `add` — creates home directory with `useradd -m`; optionally sets password via `chpasswd`
- `remove` — removes user and home directory (`userdel -r`)
- `lock` / `unlock` — disables/re-enables password login (`usermod -L/-U`)
- `passwd` — changes password via `chpasswd`

**Returns:** `CommandResult` dict.

---

#### `list_groups`

Returns all local groups from `/etc/group` with their members.

**Parameters:** `server_id`

**Returns:**
```json
[
  { "group": "docker", "gid": "999", "members": ["deploy", "ci"] }
]
```

---

### Disk and Storage

#### `disk_usage`

Returns disk usage for a path: `df` output and top-20 subdirectory sizes.

| Name | Type | Required | Default |
|---|---|---|---|
| `server_id` | string | ✅ | — |
| `path` | string | — | `/` |

**Returns:**
```json
{
  "df": "Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1        50G   21G   27G  44% /",
  "du_top": "8.5G\t/var\n4.2G\t/usr\n1.1G\t/opt"
}
```

---

#### `list_mounts`

Returns mounted filesystems: `lsblk` output and `/proc/mounts` entries.

**Parameters:** `server_id`

**Returns:**
```json
{
  "lsblk": "NAME   SIZE TYPE MOUNTPOINT FSTYPE\nsda    50G  disk\n└─sda1 50G  part /      ext4",
  "mounts": [
    { "device": "/dev/sda1", "mountpoint": "/", "fstype": "ext4", "options": "rw,relatime" }
  ]
}
```

---

#### `mount_control`

Mounts or unmounts a filesystem.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `action` | string | ✅ | — | `mount` or `umount` |
| `device` | string | — | `""` | Block device or NFS share (required for `mount`) |
| `mountpoint` | string | — | `""` | Mount point path |
| `fstype` | string | — | `""` | Filesystem type (e.g. `ext4`, `nfs`) |
| `options` | string | — | `""` | Mount options (e.g. `ro,noatime`) |
| `use_sudo` | boolean | — | `true` | |

**Notes:**
- Protected paths (`/`, `/etc`, etc.) are refused for `umount`.
- For NFS: set `device` to `server:/export` and `fstype` to `nfs`.

---

### Cron

#### `list_crontabs`

Lists crontab entries for a user and all files under `/etc/cron.d`.

| Name | Type | Required | Default |
|---|---|---|---|
| `server_id` | string | ✅ | — |
| `user` | string | — | `""` (current user) |

**Returns:**
```json
{
  "user_crontab": "0 2 * * * /opt/backup.sh",
  "cron_d": "# /etc/cron.d/logrotate\n..."
}
```

---

#### `add_cron_job`

Appends a new cron job to a user's crontab.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `schedule` | string | ✅ | — | 5-field cron expression (e.g. `0 2 * * *`) |
| `command` | string | ✅ | — | Command to run |
| `user` | string | — | `""` | Target user (defaults to current user) |

**Notes:** The schedule is validated to have exactly 5 whitespace-separated fields.

---

#### `remove_cron_job`

Removes all crontab lines matching a pattern from a user's crontab.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `pattern` | string | ✅ | — | Substring or pattern to match for removal (`grep -v`) |
| `user` | string | — | `""` | |

> **Note:** All matching lines are removed. Make the pattern specific enough to
> avoid accidental removal.

---

### SSL / TLS

#### `check_cert`

Inspects a TLS certificate. Works with live hostnames (connects via `openssl s_client`)
or local PEM files.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | Server from which to run the check |
| `target` | string | ✅ | — | Hostname (e.g. `example.com`) or absolute PEM file path |
| `port` | integer | — | `443` | TLS port (hostname targets only) |

**Returns:**
```json
{
  "target": "example.com",
  "port": 443,
  "subject": "subject=CN=example.com",
  "issuer": "issuer=C=US, O=Let's Encrypt, CN=R11",
  "not_before": "notBefore=Mar 15 00:00:00 2024 GMT",
  "not_after": "notAfter=Jun 13 23:59:59 2024 GMT",
  "fingerprint": "SHA256 Fingerprint=AA:BB:CC:...",
  "raw": "..."
}
```

**Use cases:**
- Detect certificates about to expire (`not_after` field)
- Verify the correct certificate is served after a renewal
- Inspect certificates on internal services not reachable from your local machine

---

### Git

#### `git_status`

Returns branch name, working-tree status, remotes, and ahead/behind counts
for a remote git repository.

| Name | Type | Required | Description |
|---|---|---|---|
| `server_id` | string | ✅ | |
| `repo_path` | string | ✅ | Absolute path to the git repository on the server |

**Returns:**
```json
{
  "branch": "main",
  "status": " M src/app.py\n?? temp.log",
  "remote": "origin\tgit@github.com:user/repo.git (fetch)\n...",
  "ahead_behind": "0\t2"
}
```

`ahead_behind` shows `<behind>\t<ahead>` commits relative to the tracking branch.

---

#### `git_pull`

Runs `git pull` on a remote repository.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `repo_path` | string | ✅ | — | |
| `remote` | string | — | `origin` | |
| `branch` | string | — | `""` | Leave empty to use the tracking branch |

**Returns:** `CommandResult` dict. Timeout is 120 seconds.

---

#### `git_log`

Returns the last N commits of a remote repository.

| Name | Type | Required | Default |
|---|---|---|---|
| `server_id` | string | ✅ | — |
| `repo_path` | string | ✅ | — |
| `count` | integer | — | `10` |

**Returns:**
```json
[
  {
    "hash": "a1b2c3d4e5f6...",
    "author": "Alice",
    "email": "alice@example.com",
    "date": "2024-05-20 14:30:00 +0200",
    "message": "fix: correct nginx upstream timeout"
  }
]
```

---

### Web Servers

#### `nginx_control`

Controls Nginx or inspects its configuration. Works with both `systemctl` and
legacy `service` init systems.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `action` | string | ✅ | — | `status`, `start`, `stop`, `restart`, `reload`, `test`, `list-vhosts` |
| `use_sudo` | boolean | — | `true` | |

**Action details:**
- `test` — runs `nginx -t`; returns config syntax check result
- `list-vhosts` — lists `sites-enabled/`, `conf.d/`, and all `server_name` directives
- `status` — returns `systemctl status nginx` output
- Others — pass the action directly to `systemctl` / `service`

**Returns:**
```json
{ "action": "test", "output": "nginx: configuration file /etc/nginx/nginx.conf test is successful", "ok": true }
```

---

#### `apache_control`

Controls Apache (`httpd` on RHEL/CentOS, `apache2` on Debian/Ubuntu). Auto-detects
the correct service name.

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_id` | string | ✅ | — | |
| `action` | string | ✅ | — | `status`, `start`, `stop`, `restart`, `reload`, `test`, `list-vhosts` |
| `use_sudo` | boolean | — | `true` | |

**Action details:**
- `test` — runs `apache2ctl -t` / `apachectl -t` / `httpd -t`
- `list-vhosts` — runs `apache2ctl -S` to list all virtual host configurations

**Returns:** same structure as `nginx_control`.

---

### Multi-Server

#### `broadcast_command`

Runs the same shell command on multiple servers **in parallel** and aggregates
the results. Useful for fleet-wide checks (e.g. disk usage across all nodes) or
bulk operations (e.g. pulling updated configs).

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `server_ids` | array of strings | ✅ | — | List of server IDs to target |
| `command` | string | ✅ | — | Shell command to execute on each server |
| `use_sudo` | boolean | — | `false` | |
| `timeout` | integer | — | `60` | Per-server timeout in seconds |

**Returns:**
```json
[
  { "server_id": "1", "stdout": "web01\n", "stderr": "", "exit_code": 0, "ok": true },
  { "server_id": "2", "stdout": "db01\n",  "stderr": "", "exit_code": 0, "ok": true },
  { "server_id": "3", "error": "Connection refused", "ok": false }
]
```

**Notes:**
- All servers are contacted concurrently using a thread pool.
- Failed connections are reported as `{ "error": "...", "ok": false }` without
  aborting the other requests.
- Use `list_servers` first to get valid server IDs.

---

## Authentication

Three authentication methods are supported, in priority order:

1. **SSH private key** (`SERVER_N_KEY_PATH`): the key file must be readable by
   the process running `server.py`. Passphrase-protected keys are supported if
   the key is loaded in `ssh-agent`.
2. **Password** (`SERVER_N_PASSWORD`): sent securely over the encrypted SSH
   channel.
3. **SSH agent** (automatic): if neither `KEY_PATH` nor `PASSWORD` is set,
   `paramiko` tries to contact a running `ssh-agent`.

### Sudo escalation

When `use_sudo=true` is passed to a tool and the configured user is **not** `root`:

1. A PTY channel is opened (required for sudo's password prompt).
2. The command is wrapped as `sudo -S sh -c '<command>'`.
3. `SERVER_N_SUDO_PASSWORD` (falling back to `SERVER_N_PASSWORD`) is written to
   the channel's stdin followed by a newline.

If neither password is set, sudo will block waiting for input and the command
will time out.

---

## Security Considerations

| Risk | Mitigation |
|---|---|
| Credentials in `.env` | `.gitignore` excludes `.env`; restrict file permissions (`chmod 600 .env`) |
| Host key verification disabled | `AutoAddPolicy` is used for simplicity — in production replace with `RejectPolicy` and a known-hosts file |
| Command injection via tool arguments | All shell arguments are wrapped with `_shell_quote()` (single-quote escaping); service/container/user names are validated with strict regexes |
| Dangerous `delete_path` / `umount` calls | A hardcoded blocklist refuses operations on `/`, `/etc`, `/bin`, and other critical paths |
| Broad `execute_command` | Runs arbitrary commands as the configured user; restrict MCP server access to trusted clients only |
| `user_control` with passwords | Passwords are passed via `chpasswd` stdin, not in command arguments; still avoid weak passwords |
| `broadcast_command` blast radius | A single call can affect every server simultaneously; always specify only the intended `server_ids` |
| Package / firewall changes | Irreversible or system-wide impact; ensure the MCP client is trusted before allowing these operations |
