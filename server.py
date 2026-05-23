#!/usr/bin/env python3
"""MCP server for SSH access to Linux servers.

Transport  : stdio  (JSON-RPC 2.0 managed by the mcp library)
Protocol   : MCP 2024-11-05
Auth       : credentials loaded from .env via python-dotenv
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

import ssh_manager as ssh

app = Server("linux-ssh-mcp")


# ──────────────────────────────────────────────────────────────────────────────
# Tool registry
# ──────────────────────────────────────────────────────────────────────────────

@app.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """Return the full list of tools exposed by this MCP server."""
    return [

        # ── Core ──────────────────────────────────────────────────────────────
        types.Tool(
            name="list_servers",
            description=(
                "List all configured SSH servers "
                "(id, label, host, port, user, auth type). "
                "No credentials are returned."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="execute_command",
            description=(
                "Execute a shell command on a remote server via SSH. "
                "Set use_sudo=true to run with sudo (ignored for root users). "
                "Returns stdout, stderr, exit_code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "description": "Server ID from list_servers.",
                    },
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute.",
                    },
                    "use_sudo": {"type": "boolean", "default": False},
                    "timeout": {
                        "type": "integer",
                        "default": 60,
                        "description": "Timeout in seconds.",
                    },
                },
                "required": ["server_id", "command"],
            },
        ),

        # ── File operations ───────────────────────────────────────────────────
        types.Tool(
            name="read_file",
            description=(
                "Read the content of a remote file via SFTP. "
                "Returns the file content as text."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "path": {
                        "type": "string",
                        "description": "Absolute remote path.",
                    },
                },
                "required": ["server_id", "path"],
            },
        ),
        types.Tool(
            name="write_file",
            description=(
                "Write (overwrite) a remote file via SFTP. "
                "Creates the file if it does not exist."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["server_id", "path", "content"],
            },
        ),
        types.Tool(
            name="get_file_stat",
            description=(
                "Return detailed metadata for a remote path: "
                "size, permissions, owner UID/GID, timestamps."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["server_id", "path"],
            },
        ),
        types.Tool(
            name="delete_path",
            description=(
                "Delete a file or directory on the remote server. "
                "Set recursive=true to remove a non-empty directory. "
                "Protected paths (/, /etc, /bin, etc.) are refused."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "path": {
                        "type": "string",
                        "description": "Absolute path to delete.",
                    },
                    "recursive": {"type": "boolean", "default": False},
                    "use_sudo": {"type": "boolean", "default": False},
                },
                "required": ["server_id", "path"],
            },
        ),
        types.Tool(
            name="create_directory",
            description=(
                "Create a directory (and all parent directories) "
                "on the remote server."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "path": {"type": "string"},
                    "use_sudo": {"type": "boolean", "default": False},
                },
                "required": ["server_id", "path"],
            },
        ),
        types.Tool(
            name="upload_file",
            description=(
                "Upload a local file to the remote server via SFTP. "
                "'local_path' is resolved on the machine running this MCP server."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "local_path": {
                        "type": "string",
                        "description": "Absolute local path.",
                    },
                    "remote_path": {
                        "type": "string",
                        "description": "Absolute remote destination path.",
                    },
                },
                "required": ["server_id", "local_path", "remote_path"],
            },
        ),
        types.Tool(
            name="download_file",
            description=(
                "Download a remote file to the local filesystem via SFTP. "
                "If 'local_path' is a directory the original filename is preserved."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "remote_path": {"type": "string"},
                    "local_path": {
                        "type": "string",
                        "description": "Absolute local destination path or directory.",
                    },
                },
                "required": ["server_id", "remote_path", "local_path"],
            },
        ),
        types.Tool(
            name="list_directory",
            description=(
                "List entries in a remote directory via SFTP. "
                "Returns name, type, size, permissions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "path": {"type": "string", "default": "/"},
                },
                "required": ["server_id"],
            },
        ),

        # ── File search ───────────────────────────────────────────────────────
        types.Tool(
            name="search_files",
            description=(
                "Search for files under a remote path using 'find'. "
                "Supports glob patterns, type filter, and modification date filter."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "path": {
                        "type": "string",
                        "description": "Base directory to search in.",
                    },
                    "name_pattern": {
                        "type": "string",
                        "default": "*",
                        "description": "Shell glob (e.g. '*.log').",
                    },
                    "file_type": {
                        "type": "string",
                        "enum": ["file", "directory", "any"],
                        "default": "any",
                    },
                    "modified_within_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "If > 0, only files modified in the last N days.",
                    },
                    "max_results": {"type": "integer", "default": 100},
                },
                "required": ["server_id", "path"],
            },
        ),
        types.Tool(
            name="grep_files",
            description=(
                "Recursively search for a text pattern inside files "
                "under a remote directory."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "path": {"type": "string"},
                    "pattern": {
                        "type": "string",
                        "description": "Regex or literal string to search.",
                    },
                    "file_glob": {
                        "type": "string",
                        "default": "*",
                        "description": "File name filter (e.g. '*.conf').",
                    },
                    "case_insensitive": {"type": "boolean", "default": False},
                    "max_results": {"type": "integer", "default": 100},
                },
                "required": ["server_id", "path", "pattern"],
            },
        ),

        # ── System information ────────────────────────────────────────────────
        types.Tool(
            name="get_system_info",
            description=(
                "Collect system information: hostname, uname, uptime, "
                "CPU cores, memory, disk usage, OS release."
            ),
            inputSchema={
                "type": "object",
                "properties": {"server_id": {"type": "string"}},
                "required": ["server_id"],
            },
        ),
        types.Tool(
            name="get_network_info",
            description=(
                "Return network configuration: IP addresses, "
                "listening ports (ss -tlnp), routing table, DNS resolvers."
            ),
            inputSchema={
                "type": "object",
                "properties": {"server_id": {"type": "string"}},
                "required": ["server_id"],
            },
        ),
        types.Tool(
            name="check_port",
            description=(
                "Check whether a TCP port is reachable from the remote server "
                "(uses nc or bash /dev/tcp)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "host": {
                        "type": "string",
                        "description": (
                            "Target hostname or IP. "
                            "Use 'localhost' for local services."
                        ),
                    },
                    "port": {"type": "integer"},
                    "timeout": {"type": "integer", "default": 5},
                },
                "required": ["server_id", "host", "port"],
            },
        ),

        # ── Process management ────────────────────────────────────────────────
        types.Tool(
            name="list_processes",
            description=(
                "List running processes, optionally filtered by a pattern "
                "matched against the command line."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "pattern": {
                        "type": "string",
                        "default": "",
                        "description": "Optional substring filter.",
                    },
                },
                "required": ["server_id"],
            },
        ),
        types.Tool(
            name="kill_process",
            description=(
                "Send a signal to a process. "
                "If 'target' is a number it is treated as a PID; "
                "otherwise pkill -f is used. "
                "Signals: TERM (default), KILL, HUP, INT, USR1, USR2, STOP, CONT."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "target": {
                        "type": "string",
                        "description": "PID (number) or process name pattern.",
                    },
                    "signal": {"type": "string", "default": "TERM"},
                    "use_sudo": {"type": "boolean", "default": False},
                },
                "required": ["server_id", "target"],
            },
        ),

        # ── Service management ────────────────────────────────────────────────
        types.Tool(
            name="service_control",
            description=(
                "Control a systemd service. "
                "Actions: start, stop, restart, reload, enable, disable, "
                "status, is-active, is-enabled, mask, unmask."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "service": {
                        "type": "string",
                        "description": "Service name (e.g. 'nginx', 'postgresql').",
                    },
                    "action": {
                        "type": "string",
                        "enum": [
                            "start", "stop", "restart", "reload",
                            "enable", "disable", "status",
                            "is-active", "is-enabled", "mask", "unmask",
                        ],
                    },
                    "use_sudo": {"type": "boolean", "default": True},
                },
                "required": ["server_id", "service", "action"],
            },
        ),
        types.Tool(
            name="list_services",
            description="List systemd services filtered by state.",
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "state": {
                        "type": "string",
                        "enum": ["all", "running", "failed", "inactive"],
                        "default": "all",
                    },
                },
                "required": ["server_id"],
            },
        ),

        # ── Log management ────────────────────────────────────────────────────
        types.Tool(
            name="tail_log",
            description=(
                "Read the last N lines of a log file or a systemd journal. "
                "Provide 'path' for a file or 'unit' for a systemd service "
                "(e.g. 'nginx')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "path": {
                        "type": "string",
                        "default": "",
                        "description": "Absolute path to a log file.",
                    },
                    "unit": {
                        "type": "string",
                        "default": "",
                        "description": "Systemd service name for journalctl.",
                    },
                    "lines": {"type": "integer", "default": 50},
                },
                "required": ["server_id"],
            },
        ),
        types.Tool(
            name="grep_logs",
            description=(
                "Search for a pattern inside a remote log file. "
                "Returns matching lines."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "path": {"type": "string"},
                    "pattern": {"type": "string"},
                    "case_insensitive": {"type": "boolean", "default": False},
                    "max_lines": {"type": "integer", "default": 200},
                },
                "required": ["server_id", "path", "pattern"],
            },
        ),

        # ── Docker ────────────────────────────────────────────────────────────
        types.Tool(
            name="docker_ps",
            description=(
                "List Docker containers. "
                "Set all_containers=true to include stopped containers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "all_containers": {"type": "boolean", "default": False},
                },
                "required": ["server_id"],
            },
        ),
        types.Tool(
            name="docker_logs",
            description="Return the last N log lines of a Docker container.",
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "container": {
                        "type": "string",
                        "description": "Container name or ID.",
                    },
                    "lines": {"type": "integer", "default": 50},
                    "use_sudo": {"type": "boolean", "default": False},
                },
                "required": ["server_id", "container"],
            },
        ),
        types.Tool(
            name="docker_control",
            description=(
                "Control a Docker container. "
                "Actions: start, stop, restart, pause, unpause, kill, rm."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "container": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": [
                            "start", "stop", "restart",
                            "pause", "unpause", "kill", "rm",
                        ],
                    },
                    "use_sudo": {"type": "boolean", "default": False},
                },
                "required": ["server_id", "container", "action"],
            },
        ),

        # ── Package management ────────────────────────────────────────────────
        types.Tool(
            name="list_packages",
            description=(
                "List installed packages. "
                "Auto-detects the package manager "
                "(dnf/yum/apt/apk/zypper/pacman). "
                "Optional pattern filter."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "pattern": {"type": "string", "default": ""},
                },
                "required": ["server_id"],
            },
        ),
        types.Tool(
            name="package_control",
            description=(
                "Install, remove, or update a package. "
                "Actions: install, remove, update (specific package), "
                "upgrade (all packages). "
                "Auto-detects the package manager."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "package": {
                        "type": "string",
                        "description": "Package name. Leave empty for action=upgrade.",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["install", "remove", "update", "upgrade"],
                    },
                    "use_sudo": {"type": "boolean", "default": True},
                },
                "required": ["server_id", "action"],
            },
        ),

        # ── Monitoring ────────────────────────────────────────────────────────
        types.Tool(
            name="watch_metrics",
            description=(
                "Collect CPU load, memory, and disk samples at regular intervals. "
                "Returns a list of time-stamped data points."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "samples": {
                        "type": "integer",
                        "default": 3,
                        "description": "Number of samples to collect (1-10).",
                    },
                    "interval": {
                        "type": "integer",
                        "default": 2,
                        "description": "Seconds between samples (1-30).",
                    },
                },
                "required": ["server_id"],
            },
        ),
        types.Tool(
            name="top_processes",
            description="Return the top-N processes sorted by CPU or memory usage.",
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "count": {"type": "integer", "default": 10},
                    "sort_by": {
                        "type": "string",
                        "enum": ["cpu", "mem"],
                        "default": "cpu",
                    },
                },
                "required": ["server_id"],
            },
        ),

        # ── Firewall ──────────────────────────────────────────────────────────
        types.Tool(
            name="firewall_rules",
            description=(
                "Return current firewall rules from ufw, firewalld, and iptables "
                "(whichever is available)."
            ),
            inputSchema={
                "type": "object",
                "properties": {"server_id": {"type": "string"}},
                "required": ["server_id"],
            },
        ),
        types.Tool(
            name="firewall_control",
            description=(
                "Open or close a TCP/UDP port using the available firewall manager "
                "(ufw / firewall-cmd / iptables)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["allow", "deny"],
                    },
                    "port": {"type": "integer"},
                    "protocol": {
                        "type": "string",
                        "enum": ["tcp", "udp"],
                        "default": "tcp",
                    },
                    "use_sudo": {"type": "boolean", "default": True},
                },
                "required": ["server_id", "action", "port"],
            },
        ),

        # ── Network tools ─────────────────────────────────────────────────────
        types.Tool(
            name="ping_host",
            description="Ping a host from the remote server.",
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "host": {"type": "string"},
                    "count": {"type": "integer", "default": 4},
                },
                "required": ["server_id", "host"],
            },
        ),
        types.Tool(
            name="traceroute_host",
            description=(
                "Run traceroute (or tracepath) from the remote server to a host."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "host": {"type": "string"},
                    "max_hops": {"type": "integer", "default": 20},
                },
                "required": ["server_id", "host"],
            },
        ),

        # ── User management ───────────────────────────────────────────────────
        types.Tool(
            name="list_users",
            description="Return all local users from /etc/passwd.",
            inputSchema={
                "type": "object",
                "properties": {"server_id": {"type": "string"}},
                "required": ["server_id"],
            },
        ),
        types.Tool(
            name="user_control",
            description=(
                "Manage a local user account. "
                "Actions: add, remove, lock, unlock, passwd."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "username": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "lock", "unlock", "passwd"],
                    },
                    "password": {
                        "type": "string",
                        "default": "",
                        "description": "Required for action=add (optional) and action=passwd.",
                    },
                    "shell": {
                        "type": "string",
                        "default": "",
                        "description": "Login shell for action=add.",
                    },
                    "use_sudo": {"type": "boolean", "default": True},
                },
                "required": ["server_id", "username", "action"],
            },
        ),
        types.Tool(
            name="list_groups",
            description="Return all local groups and their members from /etc/group.",
            inputSchema={
                "type": "object",
                "properties": {"server_id": {"type": "string"}},
                "required": ["server_id"],
            },
        ),

        # ── Disk and storage ──────────────────────────────────────────────────
        types.Tool(
            name="disk_usage",
            description=(
                "Return disk usage for a path: df output and top-20 "
                "subdirectory sizes (du -sh)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "path": {"type": "string", "default": "/"},
                },
                "required": ["server_id"],
            },
        ),
        types.Tool(
            name="list_mounts",
            description=(
                "Return mounted filesystems: lsblk output and /proc/mounts entries."
            ),
            inputSchema={
                "type": "object",
                "properties": {"server_id": {"type": "string"}},
                "required": ["server_id"],
            },
        ),
        types.Tool(
            name="mount_control",
            description="Mount or unmount a filesystem.",
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["mount", "umount"],
                    },
                    "device": {
                        "type": "string",
                        "default": "",
                        "description": "Block device or remote share (required for mount).",
                    },
                    "mountpoint": {
                        "type": "string",
                        "default": "",
                        "description": "Mount point path.",
                    },
                    "fstype": {"type": "string", "default": ""},
                    "options": {"type": "string", "default": ""},
                    "use_sudo": {"type": "boolean", "default": True},
                },
                "required": ["server_id", "action"],
            },
        ),

        # ── Cron ──────────────────────────────────────────────────────────────
        types.Tool(
            name="list_crontabs",
            description=(
                "List crontab entries for a user (or current user) "
                "and files under /etc/cron.d."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "user": {"type": "string", "default": ""},
                },
                "required": ["server_id"],
            },
        ),
        types.Tool(
            name="add_cron_job",
            description=(
                "Append a new cron job to a user's crontab. "
                "schedule must have exactly 5 fields (e.g. '0 2 * * *')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "schedule": {
                        "type": "string",
                        "description": "5-field cron schedule (e.g. '*/5 * * * *').",
                    },
                    "command": {"type": "string"},
                    "user": {"type": "string", "default": ""},
                },
                "required": ["server_id", "schedule", "command"],
            },
        ),
        types.Tool(
            name="remove_cron_job",
            description=(
                "Remove all crontab lines matching a pattern from a user's crontab."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "pattern": {
                        "type": "string",
                        "description": "Substring or pattern to match for removal.",
                    },
                    "user": {"type": "string", "default": ""},
                },
                "required": ["server_id", "pattern"],
            },
        ),

        # ── SSL / TLS ─────────────────────────────────────────────────────────
        types.Tool(
            name="check_cert",
            description=(
                "Check TLS certificate details (subject, issuer, expiry, fingerprint). "
                "target is a hostname or an absolute path to a PEM file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "target": {
                        "type": "string",
                        "description": "Hostname (e.g. 'example.com') or PEM file path.",
                    },
                    "port": {
                        "type": "integer",
                        "default": 443,
                        "description": "TCP port (used only for hostname targets).",
                    },
                },
                "required": ["server_id", "target"],
            },
        ),

        # ── Git ───────────────────────────────────────────────────────────────
        types.Tool(
            name="git_status",
            description=(
                "Return branch, short status, remotes, and ahead/behind counts "
                "for a remote git repository."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "repo_path": {
                        "type": "string",
                        "description": "Absolute path to the git repository on the server.",
                    },
                },
                "required": ["server_id", "repo_path"],
            },
        ),
        types.Tool(
            name="git_pull",
            description="Run git pull on a remote repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "repo_path": {"type": "string"},
                    "remote": {"type": "string", "default": "origin"},
                    "branch": {"type": "string", "default": ""},
                },
                "required": ["server_id", "repo_path"],
            },
        ),
        types.Tool(
            name="git_log",
            description="Return the last N commits of a remote git repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "repo_path": {"type": "string"},
                    "count": {"type": "integer", "default": 10},
                },
                "required": ["server_id", "repo_path"],
            },
        ),

        # ── Web servers ───────────────────────────────────────────────────────
        types.Tool(
            name="nginx_control",
            description=(
                "Control Nginx or inspect its configuration. "
                "Actions: status, reload, restart, stop, start, test, list-vhosts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": [
                            "status", "reload", "restart",
                            "stop", "start", "test", "list-vhosts",
                        ],
                    },
                    "use_sudo": {"type": "boolean", "default": True},
                },
                "required": ["server_id", "action"],
            },
        ),
        types.Tool(
            name="apache_control",
            description=(
                "Control Apache (httpd/apache2) or inspect its configuration. "
                "Actions: status, reload, restart, stop, start, test, list-vhosts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": [
                            "status", "reload", "restart",
                            "stop", "start", "test", "list-vhosts",
                        ],
                    },
                    "use_sudo": {"type": "boolean", "default": True},
                },
                "required": ["server_id", "action"],
            },
        ),

        # ── Multi-server ──────────────────────────────────────────────────────
        types.Tool(
            name="broadcast_command",
            description=(
                "Run the same shell command on multiple servers in parallel "
                "and return aggregated results."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of server IDs to target.",
                    },
                    "command": {"type": "string"},
                    "use_sudo": {"type": "boolean", "default": False},
                    "timeout": {"type": "integer", "default": 60},
                },
                "required": ["server_ids", "command"],
            },
        ),
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _j(data: Any) -> list[types.TextContent]:
    """Serialize *data* to a single TextContent item."""
    if isinstance(data, str):
        return [types.TextContent(type="text", text=data)]
    return [types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]


def _err(msg: str) -> list[types.TextContent]:
    """Return an error TextContent item."""
    return _j({"error": msg})


def _run(fn, *args, **kwargs):
    """Run a blocking ssh_manager function in the default thread-pool executor."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, lambda: fn(*args, **kwargs))


# ──────────────────────────────────────────────────────────────────────────────
# Dispatcher
# ──────────────────────────────────────────────────────────────────────────────

async def _dispatch(name: str, a: dict) -> Any:  # noqa: PLR0911
    """Route a tool call to the appropriate ssh_manager function.

    Returns raw Python data (str, dict, list); the caller wraps it in _j().
    Raises KeyError, ValueError, or FileNotFoundError on expected failures.
    """
    # ── Core ──────────────────────────────────────────────────────────────────
    if name == "list_servers":
        servers = ssh.list_servers()
        return servers or "No servers configured. Copy .env.example to .env."

    if name == "execute_command":
        r = await _run(
            ssh.execute_command,
            a["server_id"], a["command"],
            a.get("use_sudo", False), a.get("timeout", 60),
        )
        return r.as_dict()

    # ── File operations ───────────────────────────────────────────────────────
    if name == "read_file":
        return await _run(ssh.read_file, a["server_id"], a["path"])

    if name == "write_file":
        await _run(ssh.write_file, a["server_id"], a["path"], a["content"])
        return {"ok": True, "path": a["path"]}

    if name == "get_file_stat":
        return await _run(ssh.get_file_stat, a["server_id"], a["path"])

    if name == "delete_path":
        r = await _run(
            ssh.delete_path,
            a["server_id"], a["path"],
            a.get("recursive", False), a.get("use_sudo", False),
        )
        return r.as_dict()

    if name == "create_directory":
        r = await _run(
            ssh.create_directory,
            a["server_id"], a["path"], a.get("use_sudo", False),
        )
        return r.as_dict()

    if name == "upload_file":
        return await _run(
            ssh.upload_file, a["server_id"], a["local_path"], a["remote_path"],
        )

    if name == "download_file":
        return await _run(
            ssh.download_file, a["server_id"], a["remote_path"], a["local_path"],
        )

    if name == "list_directory":
        return await _run(ssh.list_directory, a["server_id"], a.get("path", "/"))

    # ── File search ───────────────────────────────────────────────────────────
    if name == "search_files":
        return await _run(
            ssh.search_files,
            a["server_id"], a["path"],
            a.get("name_pattern", "*"),
            a.get("file_type", "any"),
            a.get("modified_within_days", 0),
            a.get("max_results", 100),
        )

    if name == "grep_files":
        return await _run(
            ssh.grep_files,
            a["server_id"], a["path"], a["pattern"],
            a.get("file_glob", "*"),
            a.get("case_insensitive", False),
            a.get("max_results", 100),
        )

    # ── System information ────────────────────────────────────────────────────
    if name == "get_system_info":
        return await _run(ssh.get_system_info, a["server_id"])

    if name == "get_network_info":
        return await _run(ssh.get_network_info, a["server_id"])

    if name == "check_port":
        return await _run(
            ssh.check_port,
            a["server_id"], a["host"], a["port"], a.get("timeout", 5),
        )

    # ── Process management ────────────────────────────────────────────────────
    if name == "list_processes":
        return await _run(ssh.list_processes, a["server_id"], a.get("pattern", ""))

    if name == "kill_process":
        r = await _run(
            ssh.kill_process,
            a["server_id"], a["target"],
            a.get("signal", "TERM"), a.get("use_sudo", False),
        )
        return r.as_dict()

    # ── Service management ────────────────────────────────────────────────────
    if name == "service_control":
        r = await _run(
            ssh.service_control,
            a["server_id"], a["service"], a["action"],
            a.get("use_sudo", True),
        )
        return r.as_dict()

    if name == "list_services":
        return await _run(ssh.list_services, a["server_id"], a.get("state", "all"))

    # ── Log management ────────────────────────────────────────────────────────
    if name == "tail_log":
        return await _run(
            ssh.tail_log,
            a["server_id"],
            a.get("path", ""), a.get("lines", 50), a.get("unit", ""),
        )

    if name == "grep_logs":
        return await _run(
            ssh.grep_logs,
            a["server_id"], a["path"], a["pattern"],
            a.get("case_insensitive", False),
            a.get("max_lines", 200),
        )

    # ── Docker ────────────────────────────────────────────────────────────────
    if name == "docker_ps":
        return await _run(ssh.docker_ps, a["server_id"], a.get("all_containers", False))

    if name == "docker_logs":
        return await _run(
            ssh.docker_logs,
            a["server_id"], a["container"],
            a.get("lines", 50), a.get("use_sudo", False),
        )

    if name == "docker_control":
        r = await _run(
            ssh.docker_control,
            a["server_id"], a["container"],
            a["action"], a.get("use_sudo", False),
        )
        return r.as_dict()

    # ── Package management ────────────────────────────────────────────────────
    if name == "list_packages":
        return await _run(ssh.list_packages, a["server_id"], a.get("pattern", ""))

    if name == "package_control":
        r = await _run(
            ssh.package_control,
            a["server_id"], a.get("package", ""),
            a["action"], a.get("use_sudo", True),
        )
        return r.as_dict()

    # ── Monitoring ────────────────────────────────────────────────────────────
    if name == "watch_metrics":
        return await _run(
            ssh.watch_metrics,
            a["server_id"], a.get("samples", 3), a.get("interval", 2),
        )

    if name == "top_processes":
        return await _run(
            ssh.top_processes,
            a["server_id"], a.get("count", 10), a.get("sort_by", "cpu"),
        )

    # ── Firewall ──────────────────────────────────────────────────────────────
    if name == "firewall_rules":
        return await _run(ssh.firewall_rules, a["server_id"])

    if name == "firewall_control":
        r = await _run(
            ssh.firewall_control,
            a["server_id"], a["action"], a["port"],
            a.get("protocol", "tcp"), a.get("use_sudo", True),
        )
        return r.as_dict()

    # ── Network tools ─────────────────────────────────────────────────────────
    if name == "ping_host":
        return await _run(ssh.ping_host, a["server_id"], a["host"], a.get("count", 4))

    if name == "traceroute_host":
        return await _run(
            ssh.traceroute_host,
            a["server_id"], a["host"], a.get("max_hops", 20),
        )

    # ── User management ───────────────────────────────────────────────────────
    if name == "list_users":
        return await _run(ssh.list_users, a["server_id"])

    if name == "user_control":
        r = await _run(
            ssh.user_control,
            a["server_id"], a["username"], a["action"],
            a.get("password", ""), a.get("shell", ""), a.get("use_sudo", True),
        )
        return r.as_dict()

    if name == "list_groups":
        return await _run(ssh.list_groups, a["server_id"])

    # ── Disk and storage ──────────────────────────────────────────────────────
    if name == "disk_usage":
        return await _run(ssh.disk_usage, a["server_id"], a.get("path", "/"))

    if name == "list_mounts":
        return await _run(ssh.list_mounts, a["server_id"])

    if name == "mount_control":
        r = await _run(
            ssh.mount_control,
            a["server_id"], a["action"],
            a.get("device", ""), a.get("mountpoint", ""),
            a.get("fstype", ""), a.get("options", ""),
            a.get("use_sudo", True),
        )
        return r.as_dict()

    # ── Cron ──────────────────────────────────────────────────────────────────
    if name == "list_crontabs":
        return await _run(ssh.list_crontabs, a["server_id"], a.get("user", ""))

    if name == "add_cron_job":
        r = await _run(
            ssh.add_cron_job,
            a["server_id"], a["schedule"], a["command"], a.get("user", ""),
        )
        return r.as_dict()

    if name == "remove_cron_job":
        r = await _run(
            ssh.remove_cron_job,
            a["server_id"], a["pattern"], a.get("user", ""),
        )
        return r.as_dict()

    # ── SSL / TLS ─────────────────────────────────────────────────────────────
    if name == "check_cert":
        return await _run(ssh.check_cert, a["server_id"], a["target"], a.get("port", 443))

    # ── Git ───────────────────────────────────────────────────────────────────
    if name == "git_status":
        return await _run(ssh.git_status, a["server_id"], a["repo_path"])

    if name == "git_pull":
        r = await _run(
            ssh.git_pull,
            a["server_id"], a["repo_path"],
            a.get("remote", "origin"), a.get("branch", ""),
        )
        return r.as_dict()

    if name == "git_log":
        return await _run(ssh.git_log, a["server_id"], a["repo_path"], a.get("count", 10))

    # ── Web servers ───────────────────────────────────────────────────────────
    if name == "nginx_control":
        return await _run(
            ssh.nginx_control, a["server_id"], a["action"], a.get("use_sudo", True),
        )

    if name == "apache_control":
        return await _run(
            ssh.apache_control, a["server_id"], a["action"], a.get("use_sudo", True),
        )

    # ── Multi-server ──────────────────────────────────────────────────────────
    if name == "broadcast_command":
        return await _run(
            ssh.broadcast_command,
            a["server_ids"], a["command"],
            a.get("use_sudo", False), a.get("timeout", 60),
        )

    return {"error": f"Unknown tool: '{name}'"}


@app.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any]
) -> list[types.TextContent]:
    """Dispatch an MCP tool call and return the result as TextContent."""
    try:
        result = await _dispatch(name, arguments)
        return _j(result)
    except KeyError as exc:
        return _err(f"Server not found: {exc}")
    except (ValueError, FileNotFoundError) as exc:
        return _err(str(exc))
    except RuntimeError as exc:
        return _err(str(exc))


# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    """Start the MCP server on stdio."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
