#!/usr/bin/env python3
"""Linux SSH AI Agent – interactive multi-provider agent.

Uso:
    python3.12 agent.py [--provider PROVIDER] [--model MODEL]

Providers supportati: openrouter | groq | gemini | cloudflare | cerebras | mistral | ollama

Le credenziali vengono caricate da .env tramite python-dotenv.
Ogni provider può essere selezionato via variabile d'ambiente AGENT_PROVIDER
e AGENT_MODEL, oppure tramite i flag --provider / --model.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess  # noqa: S404 – used only to spawn `docker` with a validated image name
import sys
import threading
import time
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

import ssh_manager as ssh

load_dotenv()


# ──────────────────────────────────────────────────────────────────────────────
# MCP Docker client (optional mode)
# ──────────────────────────────────────────────────────────────────────────────

# docker image names: [registry/]name[:tag] or name@digest
_DOCKER_IMAGE_RE = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9._\-/:@]*[a-zA-Z0-9])?$')


def _validate_docker_image(image: str) -> str:
    """Raise ValueError if *image* is not a well-formed Docker image reference."""
    if not _DOCKER_IMAGE_RE.match(image):
        raise ValueError(
            f"Nome immagine Docker non valido: {image!r}. "
            "Deve contenere solo lettere, cifre, '.', '-', '_', '/', ':', '@'."
        )
    return image


class McpDockerClient:
    """Synchronous MCP client that speaks JSON-RPC 2.0 over Docker stdio."""

    def __init__(self, image: str, env_file: str = ".env", ssh_key_dir: str = "~/.ssh"):
        image = _validate_docker_image(image)
        env_file_abs = os.path.abspath(env_file)
        ssh_dir = os.path.expanduser(ssh_key_dir)
        cmd = [
            "docker", "run", "--rm", "-i",
            "--env-file", env_file_abs,
            "-v", f"{ssh_dir}:/root/.ssh:ro",
            image,
        ]
        self._proc = subprocess.Popen(  # noqa: S603
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._req_id = 0
        self._lock = threading.Lock()
        self._initialize()

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _send_recv(self, method: str, params: dict) -> dict:
        req_id = self._next_id()
        request = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        with self._lock:
            self._proc.stdin.write(json.dumps(request) + "\n")
            self._proc.stdin.flush()
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    raise RuntimeError("MCP server Docker chiuso inaspettatamente")
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" not in msg:
                    continue  # skip notifications
                if msg["id"] == req_id:
                    return msg

    def _initialize(self) -> None:
        resp = self._send_recv("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "linux-mcp-agent", "version": "1.0.0"},
        })
        if "error" in resp:
            raise RuntimeError(f"MCP initialize fallito: {resp['error']}")
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        with self._lock:
            self._proc.stdin.write(json.dumps(notif) + "\n")
            self._proc.stdin.flush()

    def call_tool(self, name: str, arguments: dict) -> str:
        resp = self._send_recv("tools/call", {"name": name, "arguments": arguments})
        if "error" in resp:
            err = resp["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            return json.dumps({"error": msg})
        result = resp.get("result", {})
        content = result.get("content", [])
        if isinstance(content, list) and content:
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")
        return json.dumps(result)

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()


# Set by main() when --mcp-docker is used
_MCP_CLIENT: McpDockerClient | None = None

# ──────────────────────────────────────────────────────────────────────────────
# Provider registry
# ──────────────────────────────────────────────────────────────────────────────

PROVIDER_REGISTRY: dict[str, dict] = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_DEFAULT_MODEL",
        "default_model": "openrouter/auto",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "model_env": "GROQ_DEFAULT_MODEL",
        "default_model": "llama-3.3-70b-versatile",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
        "model_env": "GEMINI_DEFAULT_MODEL",
        "default_model": "gemini-2.0-flash",
    },
    "cloudflare": {
        # base_url costruita dinamicamente: richiede CLOUDFLARE_ACCOUNT_ID
        "base_url": None,
        "api_key_env": "CLOUDFLARE_API_KEY",
        "model_env": "CLOUDFLARE_DEFAULT_MODEL",
        "default_model": "@cf/meta/llama-3.1-8b-instruct",
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "api_key_env": "CEREBRAS_API_KEY",
        "model_env": "CEREBRAS_DEFAULT_MODEL",
        "default_model": "llama-3.3-70b",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "api_key_env": "MISTRAL_API_KEY",
        "model_env": "MISTRAL_DEFAULT_MODEL",
        "default_model": "mistral-small-latest",
    },
    "ollama": {
        # base_url da OLLAMA_BASE_URL oppure default locale
        "base_url": None,
        "api_key_env": None,
        "model_env": "OLLAMA_DEFAULT_MODEL",
        "default_model": "llama3.2",
    },
}


def build_client(provider: str) -> tuple[OpenAI, str]:
    """Costruisce l'OpenAI client e restituisce (client, model_name)."""
    cfg = PROVIDER_REGISTRY.get(provider)
    if cfg is None:
        known = ", ".join(PROVIDER_REGISTRY)
        print(f"[errore] Provider '{provider}' non valido. Disponibili: {known}", file=sys.stderr)
        sys.exit(1)

    # ── API key ────────────────────────────────────────────────────────────────
    api_key_env = cfg["api_key_env"]
    if api_key_env:
        api_key = os.getenv(api_key_env)
        if not api_key:
            print(
                f"[errore] Variabile {api_key_env} non impostata nel .env",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        api_key = "ollama"  # Ollama non richiede chiave

    # ── base URL ───────────────────────────────────────────────────────────────
    if provider == "cloudflare":
        account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")
        if not account_id:
            print(
                "[errore] CLOUDFLARE_ACCOUNT_ID non impostato nel .env",
                file=sys.stderr,
            )
            sys.exit(1)
        base_url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
        )
    elif provider == "ollama":
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    else:
        base_url = cfg["base_url"]

    # ── model ──────────────────────────────────────────────────────────────────
    model = (
        os.getenv(cfg["model_env"]) or cfg["default_model"]
    )

    client = OpenAI(api_key=api_key, base_url=base_url)
    return client, model


# ──────────────────────────────────────────────────────────────────────────────
# Tool definitions (schema OpenAI function calling)
# ──────────────────────────────────────────────────────────────────────────────

def _tool(name: str, description: str, properties: dict, required: list) -> dict:
    """Shorthand for building an OpenAI function-calling tool dict."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


_SID = {"type": "string", "description": "Server ID from list_servers."}

TOOLS: list[dict] = [
    # ── Core ──────────────────────────────────────────────────────────────────
    _tool("list_servers",
          "List all configured SSH servers (id, label, host, port, user, auth).",
          {}, []),
    _tool("execute_command",
          "Run a shell command on a remote server. use_sudo=true for sudo. Returns stdout/stderr/exit_code.",
          {"server_id": _SID,
           "command": {"type": "string"},
           "use_sudo": {"type": "boolean", "default": False},
           "timeout": {"type": "integer", "default": 60}},
          ["server_id", "command"]),

    # ── File operations ───────────────────────────────────────────────────────
    _tool("read_file",
          "Read content of a remote file via SFTP.",
          {"server_id": _SID, "path": {"type": "string"}},
          ["server_id", "path"]),
    _tool("write_file",
          "Write (overwrite) a remote file via SFTP.",
          {"server_id": _SID,
           "path": {"type": "string"},
           "content": {"type": "string"}},
          ["server_id", "path", "content"]),
    _tool("get_file_stat",
          "Return metadata for a remote path: size, permissions, uid/gid, timestamps.",
          {"server_id": _SID, "path": {"type": "string"}},
          ["server_id", "path"]),
    _tool("delete_path",
          "Delete a remote file or directory. recursive=true for non-empty dirs. Protected paths refused.",
          {"server_id": _SID,
           "path": {"type": "string"},
           "recursive": {"type": "boolean", "default": False},
           "use_sudo": {"type": "boolean", "default": False}},
          ["server_id", "path"]),
    _tool("create_directory",
          "Create a directory (mkdir -p) on the remote server.",
          {"server_id": _SID,
           "path": {"type": "string"},
           "use_sudo": {"type": "boolean", "default": False}},
          ["server_id", "path"]),
    _tool("upload_file",
          "Upload a local file to the remote server via SFTP.",
          {"server_id": _SID,
           "local_path": {"type": "string", "description": "Absolute local path."},
           "remote_path": {"type": "string"}},
          ["server_id", "local_path", "remote_path"]),
    _tool("download_file",
          "Download a remote file to the local filesystem via SFTP.",
          {"server_id": _SID,
           "remote_path": {"type": "string"},
           "local_path": {"type": "string", "description": "Absolute local destination path or directory."}},
          ["server_id", "remote_path", "local_path"]),
    _tool("list_directory",
          "List entries in a remote directory (name, type, size, permissions).",
          {"server_id": _SID, "path": {"type": "string", "default": "/"}},
          ["server_id"]),

    # ── File search ───────────────────────────────────────────────────────────
    _tool("search_files",
          "Search files under a remote path using find. Supports glob, type, and mtime filters.",
          {"server_id": _SID,
           "path": {"type": "string"},
           "name_pattern": {"type": "string", "default": "*"},
           "file_type": {"type": "string", "enum": ["file", "directory", "any"], "default": "any"},
           "modified_within_days": {"type": "integer", "default": 0},
           "max_results": {"type": "integer", "default": 100}},
          ["server_id", "path"]),
    _tool("grep_files",
          "Recursively search for a text pattern inside files under a remote directory.",
          {"server_id": _SID,
           "path": {"type": "string"},
           "pattern": {"type": "string"},
           "file_glob": {"type": "string", "default": "*"},
           "case_insensitive": {"type": "boolean", "default": False},
           "max_results": {"type": "integer", "default": 100}},
          ["server_id", "path", "pattern"]),

    # ── System information ────────────────────────────────────────────────────
    _tool("get_system_info",
          "Collect system info: hostname, uname, uptime, CPU, memory, disk, OS release.",
          {"server_id": _SID}, ["server_id"]),
    _tool("get_network_info",
          "Return network config: IP addresses, listening ports, routing table, DNS.",
          {"server_id": _SID}, ["server_id"]),
    _tool("check_port",
          "Check if a TCP port is reachable from the remote server.",
          {"server_id": _SID,
           "host": {"type": "string", "description": "Target host. Use 'localhost' for local services."},
           "port": {"type": "integer"},
           "timeout": {"type": "integer", "default": 5}},
          ["server_id", "host", "port"]),

    # ── Process management ────────────────────────────────────────────────────
    _tool("list_processes",
          "List running processes, optionally filtered by a pattern.",
          {"server_id": _SID, "pattern": {"type": "string", "default": ""}},
          ["server_id"]),
    _tool("kill_process",
          "Send a signal to a process by PID or name. Signals: TERM, KILL, HUP, INT, USR1, USR2.",
          {"server_id": _SID,
           "target": {"type": "string", "description": "PID or process name pattern."},
           "signal": {"type": "string", "default": "TERM"},
           "use_sudo": {"type": "boolean", "default": False}},
          ["server_id", "target"]),

    # ── Service management ────────────────────────────────────────────────────
    _tool("service_control",
          "Control a systemd service. Actions: start, stop, restart, reload, enable, disable, status, is-active, is-enabled, mask, unmask.",
          {"server_id": _SID,
           "service": {"type": "string"},
           "action": {"type": "string", "enum": ["start","stop","restart","reload","enable","disable","status","is-active","is-enabled","mask","unmask"]},
           "use_sudo": {"type": "boolean", "default": True}},
          ["server_id", "service", "action"]),
    _tool("list_services",
          "List systemd services filtered by state (all/running/failed/inactive).",
          {"server_id": _SID,
           "state": {"type": "string", "enum": ["all","running","failed","inactive"], "default": "all"}},
          ["server_id"]),

    # ── Log management ────────────────────────────────────────────────────────
    _tool("tail_log",
          "Read last N lines of a log file (path) or systemd journal (unit).",
          {"server_id": _SID,
           "path": {"type": "string", "default": ""},
           "unit": {"type": "string", "default": "", "description": "Systemd service name for journalctl."},
           "lines": {"type": "integer", "default": 50}},
          ["server_id"]),
    _tool("grep_logs",
          "Search for a pattern inside a remote log file.",
          {"server_id": _SID,
           "path": {"type": "string"},
           "pattern": {"type": "string"},
           "case_insensitive": {"type": "boolean", "default": False},
           "max_lines": {"type": "integer", "default": 200}},
          ["server_id", "path", "pattern"]),

    # ── Docker ────────────────────────────────────────────────────────────────
    _tool("docker_ps",
          "List Docker containers. all_containers=true includes stopped ones.",
          {"server_id": _SID, "all_containers": {"type": "boolean", "default": False}},
          ["server_id"]),
    _tool("docker_logs",
          "Return the last N log lines of a Docker container.",
          {"server_id": _SID,
           "container": {"type": "string"},
           "lines": {"type": "integer", "default": 50},
           "use_sudo": {"type": "boolean", "default": False}},
          ["server_id", "container"]),
    _tool("docker_control",
          "Control a Docker container. Actions: start, stop, restart, pause, unpause, kill, rm.",
          {"server_id": _SID,
           "container": {"type": "string"},
           "action": {"type": "string", "enum": ["start","stop","restart","pause","unpause","kill","rm"]},
           "use_sudo": {"type": "boolean", "default": False}},
          ["server_id", "container", "action"]),

    # ── Package management ────────────────────────────────────────────────────
    _tool("list_packages",
          "List installed packages. Auto-detects dnf/yum/apt/apk/zypper/pacman. Optional pattern filter.",
          {"server_id": _SID, "pattern": {"type": "string", "default": ""}},
          ["server_id"]),
    _tool("package_control",
          "Install, remove, update, or upgrade packages. Auto-detects the package manager.",
          {"server_id": _SID,
           "package": {"type": "string", "description": "Leave empty for action=upgrade."},
           "action": {"type": "string", "enum": ["install","remove","update","upgrade"]},
           "use_sudo": {"type": "boolean", "default": True}},
          ["server_id", "action"]),

    # ── Monitoring ────────────────────────────────────────────────────────────
    _tool("watch_metrics",
          "Collect CPU load, memory, and disk samples at regular intervals. Returns time-stamped data points.",
          {"server_id": _SID,
           "samples": {"type": "integer", "default": 3, "description": "Number of data points (1-10)."},
           "interval": {"type": "integer", "default": 2, "description": "Seconds between samples (1-30)."}},
          ["server_id"]),
    _tool("top_processes",
          "Return the top-N processes sorted by CPU or memory usage.",
          {"server_id": _SID,
           "count": {"type": "integer", "default": 10},
           "sort_by": {"type": "string", "enum": ["cpu","mem"], "default": "cpu"}},
          ["server_id"]),

    # ── Firewall ──────────────────────────────────────────────────────────────
    _tool("firewall_rules",
          "Return current firewall rules (ufw / firewalld / iptables).",
          {"server_id": _SID}, ["server_id"]),
    _tool("firewall_control",
          "Open or close a TCP/UDP port using the available firewall manager (ufw/firewall-cmd/iptables).",
          {"server_id": _SID,
           "action": {"type": "string", "enum": ["allow","deny"]},
           "port": {"type": "integer"},
           "protocol": {"type": "string", "enum": ["tcp","udp"], "default": "tcp"},
           "use_sudo": {"type": "boolean", "default": True}},
          ["server_id", "action", "port"]),

    # ── Network tools ─────────────────────────────────────────────────────────
    _tool("ping_host",
          "Ping a host from the remote server.",
          {"server_id": _SID,
           "host": {"type": "string"},
           "count": {"type": "integer", "default": 4}},
          ["server_id", "host"]),
    _tool("traceroute_host",
          "Run traceroute (or tracepath) from the remote server to a host.",
          {"server_id": _SID,
           "host": {"type": "string"},
           "max_hops": {"type": "integer", "default": 20}},
          ["server_id", "host"]),

    # ── User management ───────────────────────────────────────────────────────
    _tool("list_users",
          "Return all local users from /etc/passwd (username, uid, gid, shell, home).",
          {"server_id": _SID}, ["server_id"]),
    _tool("user_control",
          "Manage a local user account. Actions: add, remove, lock, unlock, passwd.",
          {"server_id": _SID,
           "username": {"type": "string"},
           "action": {"type": "string", "enum": ["add","remove","lock","unlock","passwd"]},
           "password": {"type": "string", "default": ""},
           "shell": {"type": "string", "default": ""},
           "use_sudo": {"type": "boolean", "default": True}},
          ["server_id", "username", "action"]),
    _tool("list_groups",
          "Return all local groups and their members from /etc/group.",
          {"server_id": _SID}, ["server_id"]),

    # ── Disk and storage ──────────────────────────────────────────────────────
    _tool("disk_usage",
          "Return df output and top-20 subdirectory sizes (du -sh) for a path.",
          {"server_id": _SID, "path": {"type": "string", "default": "/"}},
          ["server_id"]),
    _tool("list_mounts",
          "Return mounted filesystems: lsblk output and /proc/mounts entries.",
          {"server_id": _SID}, ["server_id"]),
    _tool("mount_control",
          "Mount or unmount a filesystem.",
          {"server_id": _SID,
           "action": {"type": "string", "enum": ["mount","umount"]},
           "device": {"type": "string", "default": ""},
           "mountpoint": {"type": "string", "default": ""},
           "fstype": {"type": "string", "default": ""},
           "options": {"type": "string", "default": ""},
           "use_sudo": {"type": "boolean", "default": True}},
          ["server_id", "action"]),

    # ── Cron ──────────────────────────────────────────────────────────────────
    _tool("list_crontabs",
          "List crontab entries for a user and /etc/cron.d files.",
          {"server_id": _SID, "user": {"type": "string", "default": ""}},
          ["server_id"]),
    _tool("add_cron_job",
          "Append a new cron job to a user's crontab. schedule must have 5 fields (e.g. '0 2 * * *').",
          {"server_id": _SID,
           "schedule": {"type": "string"},
           "command": {"type": "string"},
           "user": {"type": "string", "default": ""}},
          ["server_id", "schedule", "command"]),
    _tool("remove_cron_job",
          "Remove all crontab lines matching a pattern from a user's crontab.",
          {"server_id": _SID,
           "pattern": {"type": "string"},
           "user": {"type": "string", "default": ""}},
          ["server_id", "pattern"]),

    # ── SSL / TLS ─────────────────────────────────────────────────────────────
    _tool("check_cert",
          "Check TLS certificate details (subject, issuer, expiry, fingerprint). "
          "target is a hostname or absolute path to a PEM file.",
          {"server_id": _SID,
           "target": {"type": "string"},
           "port": {"type": "integer", "default": 443}},
          ["server_id", "target"]),

    # ── Git ───────────────────────────────────────────────────────────────────
    _tool("git_status",
          "Return branch, short status, remotes, and ahead/behind for a remote git repository.",
          {"server_id": _SID, "repo_path": {"type": "string"}},
          ["server_id", "repo_path"]),
    _tool("git_pull",
          "Run git pull on a remote repository.",
          {"server_id": _SID,
           "repo_path": {"type": "string"},
           "remote": {"type": "string", "default": "origin"},
           "branch": {"type": "string", "default": ""}},
          ["server_id", "repo_path"]),
    _tool("git_log",
          "Return the last N commits of a remote git repository.",
          {"server_id": _SID,
           "repo_path": {"type": "string"},
           "count": {"type": "integer", "default": 10}},
          ["server_id", "repo_path"]),

    # ── Web servers ───────────────────────────────────────────────────────────
    _tool("nginx_control",
          "Control Nginx or inspect its config. Actions: status, reload, restart, stop, start, test, list-vhosts.",
          {"server_id": _SID,
           "action": {"type": "string", "enum": ["status","reload","restart","stop","start","test","list-vhosts"]},
           "use_sudo": {"type": "boolean", "default": True}},
          ["server_id", "action"]),
    _tool("apache_control",
          "Control Apache (httpd/apache2) or inspect its config. Actions: status, reload, restart, stop, start, test, list-vhosts.",
          {"server_id": _SID,
           "action": {"type": "string", "enum": ["status","reload","restart","stop","start","test","list-vhosts"]},
           "use_sudo": {"type": "boolean", "default": True}},
          ["server_id", "action"]),

    # ── Multi-server ──────────────────────────────────────────────────────────
    _tool("broadcast_command",
          "Run the same shell command on multiple servers in parallel. Returns aggregated results.",
          {"server_ids": {"type": "array", "items": {"type": "string"},
                          "description": "List of server IDs."},
           "command": {"type": "string"},
           "use_sudo": {"type": "boolean", "default": False},
           "timeout": {"type": "integer", "default": 60}},
          ["server_ids", "command"]),
]

SYSTEM_PROMPT = """\
Sei un assistente esperto di amministrazione Linux.
Hai accesso a server SSH remoti tramite i seguenti strumenti:
- Esecuzione comandi: execute_command (con sudo opzionale), broadcast_command (multi-server parallelo)
- File: read_file, write_file, get_file_stat, delete_path, create_directory, upload_file, download_file, list_directory
- Ricerca: search_files, grep_files, grep_logs
- Sistema: get_system_info, get_network_info, check_port
- Monitoraggio: watch_metrics, top_processes
- Processi: list_processes, kill_process
- Servizi systemd: service_control, list_services
- Log: tail_log, grep_logs
- Docker: docker_ps, docker_logs, docker_control
- Pacchetti: list_packages, package_control
- Firewall: firewall_rules, firewall_control
- Rete: ping_host, traceroute_host
- Utenti/gruppi: list_users, user_control, list_groups
- Disco/storage: disk_usage, list_mounts, mount_control
- Cron: list_crontabs, add_cron_job, remove_cron_job
- SSL/TLS: check_cert
- Git: git_status, git_pull, git_log
- Web server: nginx_control, apache_control

Usa list_servers come primo passo se non conosci gli ID dei server.
Rispondi in italiano, in modo conciso. Formatta tabelle e liste in modo leggibile.\
"""


# ──────────────────────────────────────────────────────────────────────────────
# Tool executor
# ──────────────────────────────────────────────────────────────────────────────

def _run_tool(name: str, args: dict) -> str:  # noqa: PLR0911,PLR0912
    """Execute a tool and return the result as a JSON string."""
    if _MCP_CLIENT is not None:
        return _MCP_CLIENT.call_tool(name, args)
    a = args
    try:
        # ── Core ──────────────────────────────────────────────────────────────
        if name == "list_servers":
            result = ssh.list_servers()
        elif name == "execute_command":
            result = ssh.execute_command(
                a["server_id"], a["command"],
                a.get("use_sudo", False), a.get("timeout", 60),
            ).as_dict()
        # ── File operations ───────────────────────────────────────────────────
        elif name == "read_file":
            result = ssh.read_file(a["server_id"], a["path"])
        elif name == "write_file":
            ssh.write_file(a["server_id"], a["path"], a["content"])
            result = {"ok": True}
        elif name == "get_file_stat":
            result = ssh.get_file_stat(a["server_id"], a["path"])
        elif name == "delete_path":
            result = ssh.delete_path(
                a["server_id"], a["path"],
                a.get("recursive", False), a.get("use_sudo", False),
            ).as_dict()
        elif name == "create_directory":
            result = ssh.create_directory(
                a["server_id"], a["path"], a.get("use_sudo", False),
            ).as_dict()
        elif name == "upload_file":
            result = ssh.upload_file(a["server_id"], a["local_path"], a["remote_path"])
        elif name == "download_file":
            result = ssh.download_file(a["server_id"], a["remote_path"], a["local_path"])
        elif name == "list_directory":
            result = ssh.list_directory(a["server_id"], a.get("path", "/"))
        # ── File search ───────────────────────────────────────────────────────
        elif name == "search_files":
            result = ssh.search_files(
                a["server_id"], a["path"],
                a.get("name_pattern", "*"), a.get("file_type", "any"),
                a.get("modified_within_days", 0), a.get("max_results", 100),
            )
        elif name == "grep_files":
            result = ssh.grep_files(
                a["server_id"], a["path"], a["pattern"],
                a.get("file_glob", "*"), a.get("case_insensitive", False),
                a.get("max_results", 100),
            )
        # ── System information ────────────────────────────────────────────────
        elif name == "get_system_info":
            result = ssh.get_system_info(a["server_id"])
        elif name == "get_network_info":
            result = ssh.get_network_info(a["server_id"])
        elif name == "check_port":
            result = ssh.check_port(
                a["server_id"], a["host"], a["port"], a.get("timeout", 5),
            )
        # ── Process management ────────────────────────────────────────────────
        elif name == "list_processes":
            result = ssh.list_processes(a["server_id"], a.get("pattern", ""))
        elif name == "kill_process":
            result = ssh.kill_process(
                a["server_id"], a["target"],
                a.get("signal", "TERM"), a.get("use_sudo", False),
            ).as_dict()
        # ── Service management ────────────────────────────────────────────────
        elif name == "service_control":
            result = ssh.service_control(
                a["server_id"], a["service"], a["action"], a.get("use_sudo", True),
            ).as_dict()
        elif name == "list_services":
            result = ssh.list_services(a["server_id"], a.get("state", "all"))
        # ── Log management ────────────────────────────────────────────────────
        elif name == "tail_log":
            result = ssh.tail_log(
                a["server_id"],
                a.get("path", ""), a.get("lines", 50), a.get("unit", ""),
            )
        elif name == "grep_logs":
            result = ssh.grep_logs(
                a["server_id"], a["path"], a["pattern"],
                a.get("case_insensitive", False), a.get("max_lines", 200),
            )
        # ── Docker ────────────────────────────────────────────────────────────
        elif name == "docker_ps":
            result = ssh.docker_ps(a["server_id"], a.get("all_containers", False))
        elif name == "docker_logs":
            result = ssh.docker_logs(
                a["server_id"], a["container"],
                a.get("lines", 50), a.get("use_sudo", False),
            )
        elif name == "docker_control":
            result = ssh.docker_control(
                a["server_id"], a["container"],
                a["action"], a.get("use_sudo", False),
            ).as_dict()
        # ── Package management ────────────────────────────────────────────────
        elif name == "list_packages":
            result = ssh.list_packages(a["server_id"], a.get("pattern", ""))
        elif name == "package_control":
            result = ssh.package_control(
                a["server_id"], a.get("package", ""),
                a["action"], a.get("use_sudo", True),
            ).as_dict()
        # ── Monitoring ────────────────────────────────────────────────────────
        elif name == "watch_metrics":
            result = ssh.watch_metrics(a["server_id"], a.get("samples", 3), a.get("interval", 2))
        elif name == "top_processes":
            result = ssh.top_processes(a["server_id"], a.get("count", 10), a.get("sort_by", "cpu"))
        # ── Firewall ──────────────────────────────────────────────────────────
        elif name == "firewall_rules":
            result = ssh.firewall_rules(a["server_id"])
        elif name == "firewall_control":
            result = ssh.firewall_control(
                a["server_id"], a["action"], a["port"],
                a.get("protocol", "tcp"), a.get("use_sudo", True),
            ).as_dict()
        # ── Network tools ─────────────────────────────────────────────────────
        elif name == "ping_host":
            result = ssh.ping_host(a["server_id"], a["host"], a.get("count", 4))
        elif name == "traceroute_host":
            result = ssh.traceroute_host(a["server_id"], a["host"], a.get("max_hops", 20))
        # ── User management ───────────────────────────────────────────────────
        elif name == "list_users":
            result = ssh.list_users(a["server_id"])
        elif name == "user_control":
            result = ssh.user_control(
                a["server_id"], a["username"], a["action"],
                a.get("password", ""), a.get("shell", ""), a.get("use_sudo", True),
            ).as_dict()
        elif name == "list_groups":
            result = ssh.list_groups(a["server_id"])
        # ── Disk and storage ──────────────────────────────────────────────────
        elif name == "disk_usage":
            result = ssh.disk_usage(a["server_id"], a.get("path", "/"))
        elif name == "list_mounts":
            result = ssh.list_mounts(a["server_id"])
        elif name == "mount_control":
            result = ssh.mount_control(
                a["server_id"], a["action"],
                a.get("device", ""), a.get("mountpoint", ""),
                a.get("fstype", ""), a.get("options", ""),
                a.get("use_sudo", True),
            ).as_dict()
        # ── Cron ──────────────────────────────────────────────────────────────
        elif name == "list_crontabs":
            result = ssh.list_crontabs(a["server_id"], a.get("user", ""))
        elif name == "add_cron_job":
            result = ssh.add_cron_job(
                a["server_id"], a["schedule"], a["command"], a.get("user", ""),
            ).as_dict()
        elif name == "remove_cron_job":
            result = ssh.remove_cron_job(a["server_id"], a["pattern"], a.get("user", "")).as_dict()
        # ── SSL / TLS ─────────────────────────────────────────────────────────
        elif name == "check_cert":
            result = ssh.check_cert(a["server_id"], a["target"], a.get("port", 443))
        # ── Git ───────────────────────────────────────────────────────────────
        elif name == "git_status":
            result = ssh.git_status(a["server_id"], a["repo_path"])
        elif name == "git_pull":
            result = ssh.git_pull(
                a["server_id"], a["repo_path"],
                a.get("remote", "origin"), a.get("branch", ""),
            ).as_dict()
        elif name == "git_log":
            result = ssh.git_log(a["server_id"], a["repo_path"], a.get("count", 10))
        # ── Web servers ───────────────────────────────────────────────────────
        elif name == "nginx_control":
            result = ssh.nginx_control(a["server_id"], a["action"], a.get("use_sudo", True))
        elif name == "apache_control":
            result = ssh.apache_control(a["server_id"], a["action"], a.get("use_sudo", True))
        # ── Multi-server ──────────────────────────────────────────────────────
        elif name == "broadcast_command":
            result = ssh.broadcast_command(
                a["server_ids"], a["command"],
                a.get("use_sudo", False), a.get("timeout", 60),
            )
        else:
            result = {"error": f"Unknown tool: '{name}'"}
    except Exception as exc:  # noqa: BLE001
        result = {"error": f"{type(exc).__name__}: {exc}"}

    return json.dumps(result, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────────────────
# Agent loop
# ──────────────────────────────────────────────────────────────────────────────

def run_agent(client: OpenAI, model: str, user_input: str, history: list[dict]) -> str:
    """Esegue un turno dell'agent: chiama il modello in loop finché non termina
    le tool call e restituisce la risposta testuale finale."""

    history.append({"role": "user", "content": user_input})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    while True:
        # Retry up to 3 times on transient 5xx errors from the provider.
        _max_retries = 3
        for _attempt in range(_max_retries):
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )
            error_info = getattr(response, "error", None)
            is_5xx = isinstance(error_info, dict) and str(error_info.get("code", "")).startswith("5")
            if response.choices or not is_5xx:
                break
            wait = 2 ** _attempt  # 1 s, 2 s, 4 s
            print(f"  [warn] provider 5xx (attempt {_attempt+1}/{_max_retries}), retry in {wait}s…")
            time.sleep(wait)

        # Alcuni provider (es. modelli free su OpenRouter) restituiscono
        # choices=None o lista vuota in caso di errore o output malformato.
        if not response.choices:
            error_text = getattr(response, "error", None) or "risposta vuota dal provider"
            raise RuntimeError(f"Provider non ha restituito choices: {error_text}")

        msg = response.choices[0].message

        # Costruisce il dict dell'assistente senza campi None per compatibilità
        # con tutti i provider (model_dump include tool_calls/content=None)
        assistant_msg: dict = {"role": "assistant"}
        if msg.content is not None:
            assistant_msg["content"] = msg.content

        # Filtra tool_calls con id o function mancante (output corrotto)
        valid_tool_calls = [
            tc for tc in (msg.tool_calls or [])
            if tc.id and tc.function and tc.function.name
        ]
        if valid_tool_calls:
            assistant_msg["tool_calls"] = [tc.model_dump() for tc in valid_tool_calls]
        messages.append(assistant_msg)

        if valid_tool_calls:
            for tc in valid_tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}

                # Stampa la tool call nel formato richiesto
                print(f"  [tool] {tc.function.name}({json.dumps(args, ensure_ascii=False)})")

                tool_result = _run_tool(tc.function.name, args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                })
        else:
            # Risposta finale: testo
            final_text = msg.content or ""
            history.append({"role": "assistant", "content": final_text})
            return final_text


# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Linux SSH AI Agent – multi-provider"
    )
    parser.add_argument(
        "--provider",
        default=os.getenv("AGENT_PROVIDER", "openrouter"),
        choices=list(PROVIDER_REGISTRY),
        help="Provider LLM (default: AGENT_PROVIDER env oppure openrouter)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Nome del modello (override di AGENT_MODEL e del default del provider)",
    )
    _mcp_image_default = os.getenv("MCP_DOCKER_IMAGE", "lordraw/linux-mcp:latest")
    _mcp_ssh_default = os.getenv("MCP_SSH_KEY_DIR", "~/.ssh")
    parser.add_argument(
        "--mcp-docker",
        metavar="IMAGE",
        nargs="?",
        const=_mcp_image_default,
        default=None,
        help=(
            "Usa il server MCP in Docker invece di ssh_manager diretto. "
            f"IMAGE facoltativo (default da MCP_DOCKER_IMAGE o '{_mcp_image_default}')."
        ),
    )
    parser.add_argument(
        "--mcp-env-file",
        default=".env",
        metavar="PATH",
        help="File .env da montare nel container MCP (default: .env)",
    )
    parser.add_argument(
        "--mcp-ssh-dir",
        default=_mcp_ssh_default,
        metavar="DIR",
        help=f"Directory chiavi SSH da montare nel container MCP (default da MCP_SSH_KEY_DIR o '{_mcp_ssh_default}')",
    )
    return parser.parse_args()


def main() -> None:
    global _MCP_CLIENT

    args = parse_args()
    provider = args.provider

    client, model = build_client(provider)

    # --model dalla CLI ha la precedenza su tutto
    if args.model:
        model = args.model
    elif os.getenv("AGENT_MODEL"):
        model = os.getenv("AGENT_MODEL")

    # ── MCP Docker mode ────────────────────────────────────────────────────────
    # Attivato da --mcp-docker oppure automaticamente se MCP_DOCKER_IMAGE è nel .env
    mcp_image = args.mcp_docker or os.getenv("MCP_DOCKER_IMAGE")
    if mcp_image:
        print(f"[mcp] Connessione al server MCP Docker  image={mcp_image}")
        try:
            _MCP_CLIENT = McpDockerClient(
                image=mcp_image,
                env_file=args.mcp_env_file,
                ssh_key_dir=args.mcp_ssh_dir,
            )
            print("[mcp] Connesso.")
        except Exception as exc:  # noqa: BLE001
            print(f"[errore] Impossibile avviare il container MCP: {exc}", file=sys.stderr)
            sys.exit(1)

    print(f"Linux SSH AI Agent  |  provider={provider}  model={model}"
          + (f"  mcp-docker={mcp_image}" if mcp_image else ""))
    print("Type your request, or 'exit' / Ctrl-C to quit.")
    print()

    history: list[dict] = []

    try:
        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nArrivederci.")
                break

            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit", "q"}:
                print("Arrivederci.")
                break

            print()
            try:
                answer = run_agent(client, model, user_input, history)
            except Exception as exc:  # noqa: BLE001
                print(f"  [errore] {type(exc).__name__}: {exc}")
                print()
                continue

            print()
            print(answer)
            print()
    finally:
        if _MCP_CLIENT is not None:
            _MCP_CLIENT.close()


if __name__ == "__main__":
    main()
