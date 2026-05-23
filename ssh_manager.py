"""SSH connection manager: loads config from .env and provides helpers for
executing commands and transferring files on remote Linux servers via paramiko."""

from __future__ import annotations

import base64
import io
import os
import re
import stat
from dataclasses import dataclass
from typing import Optional

import paramiko
from dotenv import load_dotenv

load_dotenv()


# ──────────────────────────────────────────────────────────────────────────────
# Data types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ServerConfig:
    id: str
    label: str
    host: str
    port: int
    user: str
    password: Optional[str]
    key_path: Optional[str]
    sudo_password: Optional[str]

    @property
    def is_root(self) -> bool:
        return self.user == "root"


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def as_dict(self) -> dict:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "ok": self.ok,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Server registry
# ──────────────────────────────────────────────────────────────────────────────

def load_servers() -> dict[str, ServerConfig]:
    """Reads SERVER_N_* variables from .env and builds the server registry.
    Stops at the first N for which SERVER_N_HOST is not defined."""
    servers: dict[str, ServerConfig] = {}
    i = 1
    while True:
        host = os.getenv(f"SERVER_{i}_HOST")
        if not host:
            break
        srv_id = str(i)
        servers[srv_id] = ServerConfig(
            id=srv_id,
            label=os.getenv(f"SERVER_{i}_LABEL", f"server-{i}"),
            host=host,
            port=int(os.getenv(f"SERVER_{i}_PORT", "22")),
            user=os.getenv(f"SERVER_{i}_USER", "root"),
            password=os.getenv(f"SERVER_{i}_PASSWORD") or None,
            key_path=os.getenv(f"SERVER_{i}_KEY_PATH") or None,
            sudo_password=os.getenv(f"SERVER_{i}_SUDO_PASSWORD") or None,
        )
        i += 1
    return servers


SERVERS: dict[str, ServerConfig] = load_servers()


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_config(server_id: str) -> ServerConfig:
    cfg = SERVERS.get(server_id)
    if cfg is None:
        known = list(SERVERS.keys())
        raise KeyError(f"Server '{server_id}' not found. Available: {known}")
    return cfg


def _connect(cfg: ServerConfig) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict = {
        "hostname": cfg.host,
        "port": cfg.port,
        "username": cfg.user,
        "timeout": 30,
        "allow_agent": True,
        "look_for_keys": cfg.key_path is None,
    }
    if cfg.key_path:
        kwargs["key_filename"] = cfg.key_path
    if cfg.password:
        kwargs["password"] = cfg.password
    client.connect(**kwargs)
    return client


def _sudo_exec(client: paramiko.SSHClient, cfg: ServerConfig, command: str) -> tuple[bytes, bytes, int]:
    """Runs a command through sudo using a PTY channel."""
    sudo_pass = cfg.sudo_password or cfg.password or ""
    chan = client.get_transport().open_session()
    chan.get_pty()
    chan.exec_command(f"sudo -S sh -c {_shell_quote(command)}")
    chan.sendall((sudo_pass + "\n").encode())
    stdout_data = b""
    stderr_data = b""
    while not chan.exit_status_ready():
        if chan.recv_ready():
            stdout_data += chan.recv(4096)
        if chan.recv_stderr_ready():
            stderr_data += chan.recv_stderr(4096)
    while chan.recv_ready():
        stdout_data += chan.recv(4096)
    while chan.recv_stderr_ready():
        stderr_data += chan.recv_stderr(4096)
    exit_code = chan.recv_exit_status()
    chan.close()
    return stdout_data, stderr_data, exit_code


def _shell_quote(s: str) -> str:
    """Wraps a string in single quotes, escaping any embedded single quotes."""
    return "'" + s.replace("'", "'\\''") + "'"


# ──────────────────────────────────────────────────────────────────────────────
# Core: command execution & file transfer
# ──────────────────────────────────────────────────────────────────────────────

def execute_command(
    server_id: str,
    command: str,
    use_sudo: bool = False,
    timeout: int = 60,
) -> CommandResult:
    """Executes *command* on the specified server.

    When use_sudo=True and the user is not root the command is wrapped with
    ``sudo -S`` and the sudo_password (or SSH password) is fed via stdin on a PTY.
    """
    cfg = _get_config(server_id)
    with _connect(cfg) as client:
        if use_sudo and not cfg.is_root:
            stdout_data, stderr_data, exit_code = _sudo_exec(client, cfg, command)
        else:
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            stdin.close()
            exit_code = stdout.channel.recv_exit_status()
            stdout_data = stdout.read()
            stderr_data = stderr.read()

    return CommandResult(
        stdout=stdout_data.decode("utf-8", errors="replace"),
        stderr=stderr_data.decode("utf-8", errors="replace"),
        exit_code=exit_code,
    )


def read_file(server_id: str, remote_path: str) -> str:
    """Reads the content of a remote file via SFTP and returns it as a string."""
    cfg = _get_config(server_id)
    with _connect(cfg) as client:
        with client.open_sftp() as sftp:
            buf = io.BytesIO()
            sftp.getfo(remote_path, buf)
    return buf.getvalue().decode("utf-8", errors="replace")


def write_file(server_id: str, remote_path: str, content: str) -> None:
    """Writes *content* to the remote file *remote_path* via SFTP."""
    cfg = _get_config(server_id)
    with _connect(cfg) as client:
        with client.open_sftp() as sftp:
            buf = io.BytesIO(content.encode("utf-8"))
            sftp.putfo(buf, remote_path)


def upload_file(server_id: str, local_path: str, remote_path: str) -> dict:
    """Uploads a local file to the remote server via SFTP.

    *local_path* is resolved on the machine where this server runs.
    Returns file size in bytes.
    """
    cfg = _get_config(server_id)
    if not os.path.isfile(local_path):
        raise FileNotFoundError(f"Local file not found: {local_path}")
    with _connect(cfg) as client:
        with client.open_sftp() as sftp:
            sftp.put(local_path, remote_path)
            remote_attr = sftp.stat(remote_path)
    return {"local_path": local_path, "remote_path": remote_path, "size": remote_attr.st_size}


def download_file(server_id: str, remote_path: str, local_path: str) -> dict:
    """Downloads a remote file to the local filesystem via SFTP.

    Returns file size in bytes. If *local_path* is a directory the original
    filename is preserved inside it.
    """
    cfg = _get_config(server_id)
    if os.path.isdir(local_path):
        local_path = os.path.join(local_path, os.path.basename(remote_path))
    with _connect(cfg) as client:
        with client.open_sftp() as sftp:
            sftp.get(remote_path, local_path)
    size = os.path.getsize(local_path)
    return {"remote_path": remote_path, "local_path": local_path, "size": size}


def list_directory(server_id: str, remote_path: str) -> list[dict]:
    """Returns directory entries for *remote_path* via SFTP."""
    cfg = _get_config(server_id)
    with _connect(cfg) as client:
        with client.open_sftp() as sftp:
            entries = []
            for attr in sftp.listdir_attr(remote_path):
                is_dir = stat.S_ISDIR(attr.st_mode) if attr.st_mode else False
                is_link = stat.S_ISLNK(attr.st_mode) if attr.st_mode else False
                entries.append({
                    "name": attr.filename,
                    "type": "directory" if is_dir else ("symlink" if is_link else "file"),
                    "size": attr.st_size,
                    "permissions": oct(stat.S_IMODE(attr.st_mode)) if attr.st_mode else None,
                })
    return sorted(entries, key=lambda e: (e["type"] != "directory", e["name"]))


def get_file_stat(server_id: str, remote_path: str) -> dict:
    """Returns detailed stat information for a remote path (size, permissions,
    owner UID/GID, timestamps) via SFTP plus ``ls -la`` output."""
    cfg = _get_config(server_id)
    with _connect(cfg) as client:
        with client.open_sftp() as sftp:
            attr = sftp.stat(remote_path)
        _, stdout, _ = client.exec_command(
            f"ls -la {_shell_quote(remote_path)} 2>/dev/null; "
            f"stat {_shell_quote(remote_path)} 2>/dev/null"
        )
        stat_output = stdout.read().decode("utf-8", errors="replace").strip()

    return {
        "path": remote_path,
        "size": attr.st_size,
        "uid": attr.st_uid,
        "gid": attr.st_gid,
        "permissions": oct(stat.S_IMODE(attr.st_mode)) if attr.st_mode else None,
        "is_dir": stat.S_ISDIR(attr.st_mode) if attr.st_mode else False,
        "atime": attr.st_atime,
        "mtime": attr.st_mtime,
        "stat_output": stat_output,
    }


def delete_path(
    server_id: str,
    remote_path: str,
    recursive: bool = False,
    use_sudo: bool = False,
) -> CommandResult:
    """Deletes a file or directory on the remote server.

    *recursive=True* is required to remove a non-empty directory.
    The path must be absolute and cannot be '/' or common system roots
    as a basic safety guard.
    """
    _guard_dangerous_path(remote_path)
    flags = "-rf" if recursive else "-f"
    return execute_command(
        server_id, f"rm {flags} {_shell_quote(remote_path)}", use_sudo=use_sudo
    )


def create_directory(
    server_id: str,
    remote_path: str,
    use_sudo: bool = False,
) -> CommandResult:
    """Creates a directory (and all parent directories) on the remote server."""
    return execute_command(
        server_id, f"mkdir -p {_shell_quote(remote_path)}", use_sudo=use_sudo
    )


def _guard_dangerous_path(path: str) -> None:
    """Raises ValueError if *path* is a known dangerous filesystem root."""
    dangerous = {"/", "/etc", "/bin", "/sbin", "/usr", "/lib", "/lib64", "/boot", "/dev", "/proc", "/sys"}
    if path.rstrip("/") in dangerous:
        raise ValueError(f"Refusing to operate on protected path: {path!r}")


# ──────────────────────────────────────────────────────────────────────────────
# System information
# ──────────────────────────────────────────────────────────────────────────────

def get_system_info(server_id: str) -> dict:
    """Collects basic system information: hostname, uname, uptime, CPU, memory, disk, OS."""
    commands = {
        "hostname": "hostname -f 2>/dev/null || hostname",
        "uname": "uname -a",
        "uptime": "uptime -p 2>/dev/null || uptime",
        "cpu_cores": "nproc",
        "memory": "free -h",
        "disk": "df -h --output=source,size,used,avail,pcent,target 2>/dev/null || df -h",
        "os_release": "cat /etc/os-release 2>/dev/null | head -6",
    }
    info: dict = {}
    for key, cmd in commands.items():
        result = execute_command(server_id, cmd)
        info[key] = result.stdout.strip() if result.ok else f"[error: {result.stderr.strip()}]"
    return info


def get_network_info(server_id: str) -> dict:
    """Returns network configuration: interfaces, listening ports, and routing table."""
    cmds = {
        "interfaces": "ip -o addr show 2>/dev/null || ifconfig -a 2>/dev/null",
        "listening_ports": "ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null",
        "routing_table": "ip route show 2>/dev/null || route -n 2>/dev/null",
        "dns": "cat /etc/resolv.conf 2>/dev/null",
    }
    info: dict = {}
    for key, cmd in cmds.items():
        result = execute_command(server_id, cmd)
        info[key] = result.stdout.strip() if result.ok else f"[error: {result.stderr.strip()}]"
    return info


def check_port(
    server_id: str,
    host: str,
    port: int,
    timeout: int = 5,
) -> dict:
    """Checks whether a TCP port is reachable from the remote server.

    Uses bash /dev/tcp if nc is unavailable.
    *host* can be 'localhost' to check a local service.
    """
    cmd = (
        f"(nc -z -w{timeout} {_shell_quote(host)} {port} 2>/dev/null && echo open) || "
        f"(bash -c 'timeout {timeout} bash -c \"/dev/tcp/{host}/{port}\" 2>/dev/null' "
        f"&& echo open) || echo closed"
    )
    result = execute_command(server_id, cmd)
    state = "open" if "open" in result.stdout else "closed"
    return {"host": host, "port": port, "state": state, "raw": result.stdout.strip()}


# ──────────────────────────────────────────────────────────────────────────────
# Process management
# ──────────────────────────────────────────────────────────────────────────────

def list_processes(server_id: str, pattern: str = "") -> list[dict]:
    """Returns a list of running processes, optionally filtered by *pattern*.

    Each entry has: pid, user, cpu, mem, command.
    """
    cmd = "ps aux --no-headers" if not pattern else f"ps aux --no-headers | grep -i {_shell_quote(pattern)} | grep -v grep"
    result = execute_command(server_id, cmd)
    processes = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 10)
        if len(parts) >= 11:
            processes.append({
                "user": parts[0],
                "pid": parts[1],
                "cpu": parts[2],
                "mem": parts[3],
                "vsz": parts[4],
                "rss": parts[5],
                "stat": parts[7],
                "command": parts[10],
            })
    return processes


def kill_process(
    server_id: str,
    target: str,
    signal: str = "TERM",
    use_sudo: bool = False,
) -> CommandResult:
    """Sends *signal* to a process identified by PID or name.

    *target* is treated as a PID if it is a number, otherwise ``pkill -f``
    is used to match against the full command line.
    Supported signals: TERM (default), KILL, HUP, INT, USR1, USR2.
    """
    allowed_signals = {"TERM", "KILL", "HUP", "INT", "USR1", "USR2", "STOP", "CONT"}
    signal = signal.upper()
    if signal not in allowed_signals:
        raise ValueError(f"Signal '{signal}' not allowed. Use one of: {allowed_signals}")

    if target.isdigit():
        cmd = f"kill -{signal} {target}"
    else:
        cmd = f"pkill -{signal} -f {_shell_quote(target)}"

    return execute_command(server_id, cmd, use_sudo=use_sudo)


# ──────────────────────────────────────────────────────────────────────────────
# Service management (systemd)
# ──────────────────────────────────────────────────────────────────────────────

def service_control(
    server_id: str,
    service: str,
    action: str,
    use_sudo: bool = True,
) -> CommandResult:
    """Controls a systemd service.

    *action* must be one of: start, stop, restart, reload, enable, disable, status, is-active, is-enabled.
    For non-root users sudo is used by default.
    """
    valid_actions = {"start", "stop", "restart", "reload", "enable", "disable",
                     "status", "is-active", "is-enabled", "mask", "unmask"}
    if action not in valid_actions:
        raise ValueError(f"Action '{action}' not valid. Use: {valid_actions}")
    # Sanitize service name: only allow alphanumeric, dash, underscore, dot, @
    if not re.match(r'^[\w@.\-]+$', service):
        raise ValueError(f"Invalid service name: {service!r}")

    cmd = f"systemctl {action} {service}"
    cfg = _get_config(server_id)
    effective_sudo = use_sudo and not cfg.is_root
    return execute_command(server_id, cmd, use_sudo=effective_sudo)


def list_services(server_id: str, state: str = "all") -> list[dict]:
    """Lists systemd services filtered by *state*.

    *state*: 'all' (default), 'running', 'failed', 'inactive'.
    Each entry has: unit, load, active, sub, description.
    """
    state_filter = {
        "running": "--state=running",
        "failed": "--state=failed",
        "inactive": "--state=inactive",
        "all": "",
    }.get(state, "")
    cmd = f"systemctl list-units --type=service {state_filter} --no-pager --no-legend 2>/dev/null"
    result = execute_command(server_id, cmd)
    services = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4:
            services.append({
                "unit": parts[0],
                "load": parts[1],
                "active": parts[2],
                "sub": parts[3],
                "description": parts[4] if len(parts) > 4 else "",
            })
    return services


# ──────────────────────────────────────────────────────────────────────────────
# Log management
# ──────────────────────────────────────────────────────────────────────────────

def tail_log(
    server_id: str,
    path: str = "",
    lines: int = 50,
    unit: str = "",
) -> str:
    """Returns the last *lines* of a log file or a systemd unit journal.

    Provide either *path* (absolute path to a log file) or *unit* (systemd
    service name for ``journalctl -u``). At least one must be non-empty.
    """
    if lines < 1 or lines > 5000:
        raise ValueError("lines must be between 1 and 5000")
    if unit:
        cmd = f"journalctl -u {_shell_quote(unit)} -n {lines} --no-pager 2>/dev/null"
    elif path:
        cmd = f"tail -n {lines} {_shell_quote(path)}"
    else:
        raise ValueError("Provide either 'path' or 'unit'")
    result = execute_command(server_id, cmd)
    return result.stdout if result.ok else f"[error: {result.stderr.strip()}]"


def grep_logs(
    server_id: str,
    path: str,
    pattern: str,
    case_insensitive: bool = False,
    max_lines: int = 200,
) -> list[str]:
    """Searches for *pattern* in a log file and returns matching lines (up to *max_lines*)."""
    if max_lines < 1 or max_lines > 2000:
        raise ValueError("max_lines must be between 1 and 2000")
    flags = "-i" if case_insensitive else ""
    cmd = f"grep {flags} {_shell_quote(pattern)} {_shell_quote(path)} | head -n {max_lines}"
    result = execute_command(server_id, cmd)
    return result.stdout.splitlines()


# ──────────────────────────────────────────────────────────────────────────────
# File search
# ──────────────────────────────────────────────────────────────────────────────

def search_files(
    server_id: str,
    path: str,
    name_pattern: str = "*",
    file_type: str = "any",
    modified_within_days: int = 0,
    max_results: int = 100,
) -> list[str]:
    """Searches for files under *path* using ``find``.

    Args:
        name_pattern: Shell glob pattern (e.g. '*.log', 'nginx*').
        file_type: 'file', 'directory', or 'any'.
        modified_within_days: If > 0, only files modified in the last N days.
        max_results: Maximum number of results to return (capped at 500).
    """
    if max_results > 500:
        max_results = 500
    type_flag = {"file": "-type f", "directory": "-type d", "any": ""}.get(file_type, "")
    mtime_flag = f"-mtime -{modified_within_days}" if modified_within_days > 0 else ""
    cmd = (
        f"find {_shell_quote(path)} -name {_shell_quote(name_pattern)} "
        f"{type_flag} {mtime_flag} 2>/dev/null | head -n {max_results}"
    )
    result = execute_command(server_id, cmd)
    return [line for line in result.stdout.splitlines() if line.strip()]


def grep_files(
    server_id: str,
    path: str,
    pattern: str,
    file_glob: str = "*",
    case_insensitive: bool = False,
    max_results: int = 100,
) -> list[dict]:
    """Recursively searches for *pattern* inside files under *path*.

    Returns a list of {'file': ..., 'line': ..., 'match': ...} dicts.
    """
    if max_results > 500:
        max_results = 500
    flags = "-ri" if case_insensitive else "-r"
    cmd = (
        f"grep {flags} --include={_shell_quote(file_glob)} -n "
        f"{_shell_quote(pattern)} {_shell_quote(path)} 2>/dev/null | head -n {max_results}"
    )
    result = execute_command(server_id, cmd)
    matches = []
    for line in result.stdout.splitlines():
        # format: filepath:linenum:content
        parts = line.split(":", 2)
        if len(parts) == 3:
            matches.append({"file": parts[0], "line": parts[1], "match": parts[2]})
        elif len(parts) == 2:
            matches.append({"file": parts[0], "line": parts[1], "match": ""})
    return matches


# ──────────────────────────────────────────────────────────────────────────────
# Docker
# ──────────────────────────────────────────────────────────────────────────────

def docker_ps(server_id: str, all_containers: bool = False) -> list[dict]:
    """Returns a list of Docker containers.

    *all_containers=True* includes stopped containers (docker ps -a).
    Each entry has: id, name, image, status, ports, created.
    """
    flag = "-a" if all_containers else ""
    cmd = f'docker ps {flag} --format "{{{{.ID}}}}|{{{{.Names}}}}|{{{{.Image}}}}|{{{{.Status}}}}|{{{{.Ports}}}}|{{{{.CreatedAt}}}}" 2>/dev/null'
    result = execute_command(server_id, cmd)
    containers = []
    for line in result.stdout.splitlines():
        parts = line.split("|")
        if len(parts) == 6:
            containers.append({
                "id": parts[0],
                "name": parts[1],
                "image": parts[2],
                "status": parts[3],
                "ports": parts[4],
                "created": parts[5],
            })
    return containers


def docker_logs(
    server_id: str,
    container: str,
    lines: int = 50,
    use_sudo: bool = False,
) -> str:
    """Returns the last *lines* log lines of a Docker container."""
    if lines < 1 or lines > 5000:
        raise ValueError("lines must be between 1 and 5000")
    # Sanitize container name/id: alphanumeric, dash, underscore only
    if not re.match(r'^[\w\-]+$', container):
        raise ValueError(f"Invalid container name/id: {container!r}")
    cmd = f"docker logs --tail {lines} {container} 2>&1"
    result = execute_command(server_id, cmd, use_sudo=use_sudo)
    return result.stdout


def docker_control(
    server_id: str,
    container: str,
    action: str,
    use_sudo: bool = False,
) -> CommandResult:
    """Controls a Docker container.

    *action* must be one of: start, stop, restart, pause, unpause, kill, rm.
    """
    valid_actions = {"start", "stop", "restart", "pause", "unpause", "kill", "rm"}
    if action not in valid_actions:
        raise ValueError(f"Action '{action}' not valid. Use: {valid_actions}")
    if not re.match(r'^[\w\-]+$', container):
        raise ValueError(f"Invalid container name/id: {container!r}")
    cmd = f"docker {action} {container}"
    return execute_command(server_id, cmd, use_sudo=use_sudo)


# ──────────────────────────────────────────────────────────────────────────────
# Package management
# ──────────────────────────────────────────────────────────────────────────────

def _detect_package_manager(server_id: str) -> str:
    """Detects the available package manager on the remote system."""
    for pm in ("dnf", "yum", "apt-get", "apk", "zypper", "pacman"):
        result = execute_command(server_id, f"command -v {pm} 2>/dev/null")
        if result.ok and result.stdout.strip():
            return pm
    raise RuntimeError("No supported package manager found (dnf/yum/apt-get/apk/zypper/pacman)")


def list_packages(server_id: str, pattern: str = "") -> list[dict]:
    """Lists installed packages, optionally filtered by *pattern*.

    Auto-detects the package manager (dnf/yum/apt/apk/zypper/pacman).
    Each entry has: name, version.
    """
    pm = _detect_package_manager(server_id)
    if pm in ("dnf", "yum"):
        cmd = f"rpm -qa --queryformat '%{{NAME}} %{{VERSION}}-%{{RELEASE}}\\n'"
    elif pm == "apt-get":
        cmd = "dpkg-query -W -f='${Package} ${Version}\\n'"
    elif pm == "apk":
        cmd = "apk list --installed 2>/dev/null"
    elif pm == "zypper":
        cmd = "zypper packages --installed-only 2>/dev/null | awk -F'|' 'NR>4{print $3,$4}'"
    elif pm == "pacman":
        cmd = "pacman -Q"
    else:
        cmd = "rpm -qa"

    if pattern:
        cmd += f" | grep -i {_shell_quote(pattern)}"

    result = execute_command(server_id, cmd)
    packages = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if parts:
            packages.append({"name": parts[0], "version": parts[1] if len(parts) > 1 else ""})
    return sorted(packages, key=lambda p: p["name"])


def package_control(
    server_id: str,
    package: str,
    action: str,
    use_sudo: bool = True,
) -> CommandResult:
    """Installs, removes, or updates a package.

    *action*: 'install', 'remove', 'update' (update specific package),
              'upgrade' (upgrade all packages).
    Uses the auto-detected package manager. Runs with sudo for non-root users.
    """
    valid_actions = {"install", "remove", "update", "upgrade"}
    if action not in valid_actions:
        raise ValueError(f"Action '{action}' not valid. Use: {valid_actions}")
    # Sanitize package name
    if package and not re.match(r'^[\w.\-+]+$', package):
        raise ValueError(f"Invalid package name: {package!r}")

    pm = _detect_package_manager(server_id)
    non_interactive = "-y"

    if pm in ("dnf", "yum"):
        if action == "upgrade":
            cmd = f"{pm} {non_interactive} update"
        elif action == "update":
            cmd = f"{pm} {non_interactive} update {package}"
        elif action == "install":
            cmd = f"{pm} {non_interactive} install {package}"
        elif action == "remove":
            cmd = f"{pm} {non_interactive} remove {package}"
    elif pm == "apt-get":
        if action == "upgrade":
            cmd = f"apt-get {non_interactive} upgrade"
        elif action == "update":
            cmd = f"apt-get {non_interactive} install --only-upgrade {package}"
        elif action == "install":
            cmd = f"DEBIAN_FRONTEND=noninteractive apt-get {non_interactive} install {package}"
        elif action == "remove":
            cmd = f"apt-get {non_interactive} remove {package}"
    elif pm == "apk":
        mapping = {"install": "add", "remove": "del", "update": "upgrade", "upgrade": "upgrade"}
        apk_action = mapping[action]
        cmd = f"apk {apk_action} {package if action not in ('upgrade',) else ''}"
    else:
        cmd = f"{pm} {action} {package}"

    cfg = _get_config(server_id)
    effective_sudo = use_sudo and not cfg.is_root
    return execute_command(server_id, cmd.strip(), use_sudo=effective_sudo)


# ──────────────────────────────────────────────────────────────────────────────
# Monitoring
# ──────────────────────────────────────────────────────────────────────────────

def watch_metrics(server_id: str, samples: int = 3, interval: int = 2) -> list[dict]:
    """Collects CPU load, memory, and disk samples at regular intervals.

    Args:
        samples: Number of data points to collect (1-10).
        interval: Seconds between samples (1-30).
    Returns a list of dicts with timestamp, load averages, memory, and disk usage.
    """
    if not (1 <= samples <= 10):
        raise ValueError("samples must be between 1 and 10")
    if not (1 <= interval <= 30):
        raise ValueError("interval must be between 1 and 30")
    cmd = (
        f"for i in $(seq 1 {samples}); do "
        f"echo ---SAMPLE---; "
        f"date +%s; "
        f"cat /proc/loadavg; "
        f"free -m | awk 'NR==2{{print $2,$3,$4}}'; "
        f"df -h / | awk 'NR==2{{print $2,$3,$4,$5}}'; "
        f"[ $i -lt {samples} ] && sleep {interval}; "
        f"done"
    )
    result = execute_command(server_id, cmd, timeout=samples * interval + 15)
    samples_data: list[dict] = []
    cur: dict = {}
    stage = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if line == "---SAMPLE---":
            if cur:
                samples_data.append(cur)
            cur = {}
            stage = 1
            continue
        if stage == 1:
            try:
                cur["timestamp"] = int(line)
            except ValueError:
                pass
            stage = 2
        elif stage == 2:
            parts = line.split()
            if len(parts) >= 3:
                cur["load_1m"], cur["load_5m"], cur["load_15m"] = parts[0], parts[1], parts[2]
            stage = 3
        elif stage == 3:
            parts = line.split()
            if len(parts) >= 3:
                cur["mem_total_mb"] = parts[0]
                cur["mem_used_mb"] = parts[1]
                cur["mem_free_mb"] = parts[2]
            stage = 4
        elif stage == 4:
            parts = line.split()
            if len(parts) >= 4:
                cur["disk_total"] = parts[0]
                cur["disk_used"] = parts[1]
                cur["disk_avail"] = parts[2]
                cur["disk_pct"] = parts[3]
            stage = 0
    if cur:
        samples_data.append(cur)
    return samples_data


def top_processes(server_id: str, count: int = 10, sort_by: str = "cpu") -> list[dict]:
    """Returns the top-N processes sorted by CPU or memory usage."""
    if not (1 <= count <= 100):
        raise ValueError("count must be between 1 and 100")
    if sort_by not in ("cpu", "mem"):
        raise ValueError("sort_by must be 'cpu' or 'mem'")
    sort_col = 3 if sort_by == "cpu" else 4
    cmd = f"ps aux --no-headers | sort -rk{sort_col} | head -n {count}"
    result = execute_command(server_id, cmd)
    procs = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 10)
        if len(parts) >= 11:
            procs.append({
                "user": parts[0], "pid": parts[1],
                "cpu_pct": parts[2], "mem_pct": parts[3],
                "command": parts[10],
            })
    return procs


# ──────────────────────────────────────────────────────────────────────────────
# Firewall
# ──────────────────────────────────────────────────────────────────────────────

def firewall_rules(server_id: str) -> dict:
    """Returns current firewall rules from ufw, firewalld, and iptables."""
    results: dict = {}
    r = execute_command(server_id, "ufw status verbose 2>/dev/null")
    if r.ok and r.stdout.strip():
        results["ufw"] = r.stdout.strip()
    r = execute_command(server_id, "firewall-cmd --list-all 2>/dev/null")
    if r.ok and r.stdout.strip():
        results["firewalld"] = r.stdout.strip()
    r = execute_command(server_id, "iptables -L -n -v 2>/dev/null || iptables --list 2>/dev/null")
    if r.stdout.strip():
        results["iptables"] = r.stdout.strip()
    return results


def firewall_control(
    server_id: str,
    action: str,
    port: int,
    protocol: str = "tcp",
    use_sudo: bool = True,
) -> CommandResult:
    """Opens or closes a firewall port using the first available manager (ufw/firewalld/iptables).

    action: 'allow' or 'deny'.
    """
    if action not in ("allow", "deny"):
        raise ValueError("action must be 'allow' or 'deny'")
    if protocol not in ("tcp", "udp"):
        raise ValueError("protocol must be 'tcp' or 'udp'")
    if not (1 <= port <= 65535):
        raise ValueError("port must be between 1 and 65535")
    fw_sub = "add" if action == "allow" else "remove"
    ipt_target = "ACCEPT" if action == "allow" else "DROP"
    ipt_flag = "-A" if action == "allow" else "-D"
    cmd = (
        f"if command -v ufw >/dev/null 2>&1; then "
        f"ufw {action} {port}/{protocol}; "
        f"elif command -v firewall-cmd >/dev/null 2>&1; then "
        f"firewall-cmd --permanent --{fw_sub}-port={port}/{protocol} && firewall-cmd --reload; "
        f"else "
        f"iptables {ipt_flag} INPUT -p {protocol} --dport {port} -j {ipt_target}; "
        f"fi"
    )
    cfg = _get_config(server_id)
    return execute_command(server_id, cmd, use_sudo=use_sudo and not cfg.is_root)


# ──────────────────────────────────────────────────────────────────────────────
# Network tools
# ──────────────────────────────────────────────────────────────────────────────

def ping_host(server_id: str, host: str, count: int = 4) -> dict:
    """Runs ping from the remote server to a host."""
    if not (1 <= count <= 20):
        raise ValueError("count must be between 1 and 20")
    if not re.match(r'^[\w.\-:]+$', host):
        raise ValueError(f"Invalid host: {host!r}")
    result = execute_command(server_id, f"ping -c {count} -W 3 {_shell_quote(host)} 2>&1", timeout=count * 5 + 10)
    return {"host": host, "output": result.stdout.strip(), "ok": result.ok}


def traceroute_host(server_id: str, host: str, max_hops: int = 20) -> str:
    """Runs traceroute from the remote server to a host."""
    if not re.match(r'^[\w.\-:]+$', host):
        raise ValueError(f"Invalid host: {host!r}")
    if not (1 <= max_hops <= 30):
        raise ValueError("max_hops must be between 1 and 30")
    cmd = (
        f"traceroute -m {max_hops} {_shell_quote(host)} 2>/dev/null || "
        f"tracepath -m {max_hops} {_shell_quote(host)} 2>/dev/null"
    )
    result = execute_command(server_id, cmd, timeout=max_hops * 5 + 10)
    return result.stdout.strip() or result.stderr.strip()


# ──────────────────────────────────────────────────────────────────────────────
# User management
# ──────────────────────────────────────────────────────────────────────────────

def list_users(server_id: str) -> list[dict]:
    """Returns all local users from /etc/passwd."""
    result = execute_command(server_id, "cat /etc/passwd")
    users = []
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 7:
            users.append({
                "username": parts[0],
                "uid": parts[2],
                "gid": parts[3],
                "comment": parts[4],
                "home": parts[5],
                "shell": parts[6],
            })
    return users


def user_control(
    server_id: str,
    username: str,
    action: str,
    password: str = "",
    shell: str = "",
    use_sudo: bool = True,
) -> CommandResult:
    """Manages a local user account.

    action: add, remove, lock, unlock, passwd.
    """
    valid_actions = {"add", "remove", "lock", "unlock", "passwd"}
    if action not in valid_actions:
        raise ValueError(f"action must be one of: {valid_actions}")
    if not re.match(r'^[\w.\-]+$', username):
        raise ValueError(f"Invalid username: {username!r}")
    if action == "add":
        shell_flag = f" -s {_shell_quote(shell)}" if shell else ""
        if password:
            cmd = (
                f"useradd -m{shell_flag} {username} && "
                f"echo {_shell_quote(username + ':' + password)} | chpasswd"
            )
        else:
            cmd = f"useradd -m{shell_flag} {username}"
    elif action == "remove":
        cmd = f"userdel -r {username} 2>/dev/null || userdel {username}"
    elif action == "lock":
        cmd = f"usermod -L {username}"
    elif action == "unlock":
        cmd = f"usermod -U {username}"
    else:  # passwd
        if not password:
            raise ValueError("password is required for action=passwd")
        cmd = f"echo {_shell_quote(username + ':' + password)} | chpasswd"
    cfg = _get_config(server_id)
    return execute_command(server_id, cmd, use_sudo=use_sudo and not cfg.is_root)


def list_groups(server_id: str) -> list[dict]:
    """Returns all local groups from /etc/group."""
    result = execute_command(server_id, "cat /etc/group")
    groups = []
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 4:
            groups.append({
                "group": parts[0],
                "gid": parts[2],
                "members": [m for m in parts[3].split(",") if m],
            })
    return groups


# ──────────────────────────────────────────────────────────────────────────────
# Disk and storage
# ──────────────────────────────────────────────────────────────────────────────

def disk_usage(server_id: str, path: str = "/") -> dict:
    """Returns df output and top-20 subdirectory sizes for a path."""
    df_r = execute_command(server_id, f"df -h {_shell_quote(path)} 2>/dev/null")
    du_r = execute_command(
        server_id,
        f"du -sh {_shell_quote(path)}/* 2>/dev/null | sort -rh | head -20",
    )
    return {"df": df_r.stdout.strip(), "du_top": du_r.stdout.strip()}


def list_mounts(server_id: str) -> dict:
    """Returns mounted filesystems (lsblk output and /proc/mounts entries)."""
    lsblk_r = execute_command(
        server_id, "lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE 2>/dev/null"
    )
    mounts_r = execute_command(server_id, "cat /proc/mounts 2>/dev/null")
    mounts = []
    for line in mounts_r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4 and not parts[0].startswith("#"):
            mounts.append({
                "device": parts[0],
                "mountpoint": parts[1],
                "fstype": parts[2],
                "options": parts[3],
            })
    return {"lsblk": lsblk_r.stdout.strip(), "mounts": mounts}


def mount_control(
    server_id: str,
    action: str,
    device: str = "",
    mountpoint: str = "",
    fstype: str = "",
    options: str = "",
    use_sudo: bool = True,
) -> CommandResult:
    """Mounts or unmounts a filesystem."""
    if action not in ("mount", "umount"):
        raise ValueError("action must be 'mount' or 'umount'")
    if action == "umount":
        target = mountpoint or device
        if not target:
            raise ValueError("Provide mountpoint or device for umount")
        _guard_dangerous_path(target)
        cmd = f"umount {_shell_quote(target)}"
    else:
        if not device or not mountpoint:
            raise ValueError("device and mountpoint are required for mount")
        fstype_flag = f" -t {_shell_quote(fstype)}" if fstype else ""
        opts_flag = f" -o {_shell_quote(options)}" if options else ""
        cmd = f"mount{fstype_flag}{opts_flag} {_shell_quote(device)} {_shell_quote(mountpoint)}"
    cfg = _get_config(server_id)
    return execute_command(server_id, cmd, use_sudo=use_sudo and not cfg.is_root)


# ──────────────────────────────────────────────────────────────────────────────
# Cron
# ──────────────────────────────────────────────────────────────────────────────

def list_crontabs(server_id: str, user: str = "") -> dict:
    """Lists crontab entries for a user and files under /etc/cron.d."""
    if user and not re.match(r'^[\w.\-]+$', user):
        raise ValueError(f"Invalid username: {user!r}")
    uf = f"-u {user}" if user else ""
    cr = execute_command(server_id, f"crontab {uf} -l 2>/dev/null")
    cd = execute_command(server_id, "ls /etc/cron.d/ 2>/dev/null && cat /etc/cron.d/* 2>/dev/null")
    return {
        "user_crontab": cr.stdout.strip() or "(empty)",
        "cron_d": cd.stdout.strip() or "(empty)",
    }


def add_cron_job(server_id: str, schedule: str, command: str, user: str = "") -> CommandResult:
    """Appends a new cron job to a user's crontab.

    schedule must have exactly 5 fields (e.g. '0 2 * * *').
    """
    if user and not re.match(r'^[\w.\-]+$', user):
        raise ValueError(f"Invalid username: {user!r}")
    if len(schedule.strip().split()) != 5:
        raise ValueError("schedule must have exactly 5 fields (e.g. '0 2 * * *')")
    entry = f"{schedule} {command}"
    uf = f"-u {user}" if user else ""
    cmd = f"(crontab {uf} -l 2>/dev/null; echo {_shell_quote(entry)}) | crontab {uf} -"
    return execute_command(server_id, cmd)


def remove_cron_job(server_id: str, pattern: str, user: str = "") -> CommandResult:
    """Removes all crontab lines matching *pattern* from a user's crontab."""
    if user and not re.match(r'^[\w.\-]+$', user):
        raise ValueError(f"Invalid username: {user!r}")
    uf = f"-u {user}" if user else ""
    cmd = (
        f"crontab {uf} -l 2>/dev/null "
        f"| grep -v {_shell_quote(pattern)} "
        f"| crontab {uf} -"
    )
    return execute_command(server_id, cmd)


# ──────────────────────────────────────────────────────────────────────────────
# SSL / TLS
# ──────────────────────────────────────────────────────────────────────────────

def check_cert(server_id: str, target: str, port: int = 443) -> dict:
    """Checks TLS certificate details.

    *target* is a hostname (connects via openssl s_client) or an absolute path
    to a PEM file.
    """
    if target.startswith("/"):
        cmd = (
            f"openssl x509 -in {_shell_quote(target)} -noout "
            f"-subject -issuer -dates -fingerprint 2>&1"
        )
    else:
        if not re.match(r'^[\w.\-]+$', target):
            raise ValueError(f"Invalid hostname: {target!r}")
        cmd = (
            f"echo | openssl s_client -connect {_shell_quote(target)}:{port} "
            f"-servername {_shell_quote(target)} 2>/dev/null "
            f"| openssl x509 -noout -subject -issuer -dates -fingerprint 2>&1"
        )
    result = execute_command(server_id, cmd, timeout=15)
    info: dict = {"target": target, "port": port, "raw": result.stdout.strip()}
    for line in result.stdout.splitlines():
        ll = line.lower()
        if "subject" in ll:
            info.setdefault("subject", line.strip())
        elif "issuer" in ll:
            info.setdefault("issuer", line.strip())
        elif "notbefore" in ll:
            info["not_before"] = line.strip()
        elif "notafter" in ll:
            info["not_after"] = line.strip()
        elif "fingerprint" in ll:
            info["fingerprint"] = line.strip()
    return info


# ──────────────────────────────────────────────────────────────────────────────
# Git
# ──────────────────────────────────────────────────────────────────────────────

def git_status(server_id: str, repo_path: str) -> dict:
    """Returns branch, status, remotes, and ahead/behind counts for a repo."""
    cmds = {
        "branch": f"git -C {_shell_quote(repo_path)} branch --show-current 2>&1",
        "status": f"git -C {_shell_quote(repo_path)} status --short 2>&1",
        "remote": f"git -C {_shell_quote(repo_path)} remote -v 2>&1",
        "ahead_behind": (
            f"git -C {_shell_quote(repo_path)} rev-list --count --left-right "
            f"@{{upstream}}...HEAD 2>/dev/null || echo 'no upstream'"
        ),
    }
    info: dict = {}
    for key, cmd in cmds.items():
        r = execute_command(server_id, cmd)
        info[key] = r.stdout.strip()
    return info


def git_pull(
    server_id: str,
    repo_path: str,
    remote: str = "origin",
    branch: str = "",
) -> CommandResult:
    """Runs git pull on a remote repository."""
    branch_arg = f" {_shell_quote(branch)}" if branch else ""
    cmd = f"git -C {_shell_quote(repo_path)} pull {_shell_quote(remote)}{branch_arg} 2>&1"
    return execute_command(server_id, cmd, timeout=120)


def git_log(server_id: str, repo_path: str, count: int = 10) -> list[dict]:
    """Returns the last *count* commits of a remote repository."""
    if not (1 <= count <= 100):
        raise ValueError("count must be between 1 and 100")
    fmt = "%H|%an|%ae|%ai|%s"
    cmd = (
        f"git -C {_shell_quote(repo_path)} log -n {count} "
        f"--pretty=format:{_shell_quote(fmt)} 2>&1"
    )
    result = execute_command(server_id, cmd)
    commits = []
    for line in result.stdout.splitlines():
        parts = line.split("|", 4)
        if len(parts) == 5:
            commits.append({
                "hash": parts[0], "author": parts[1],
                "email": parts[2], "date": parts[3], "message": parts[4],
            })
    return commits


# ──────────────────────────────────────────────────────────────────────────────
# Web servers
# ──────────────────────────────────────────────────────────────────────────────

def nginx_control(server_id: str, action: str, use_sudo: bool = True) -> dict:
    """Controls Nginx or inspects its configuration.

    action: status, reload, restart, stop, start, test, list-vhosts.
    """
    valid_actions = {"status", "reload", "restart", "stop", "start", "test", "list-vhosts"}
    if action not in valid_actions:
        raise ValueError(f"action must be one of: {valid_actions}")
    cfg = _get_config(server_id)
    esudo = use_sudo and not cfg.is_root
    if action == "test":
        r = execute_command(server_id, "nginx -t 2>&1", use_sudo=esudo)
        return {"action": action, "output": r.stdout.strip() or r.stderr.strip(), "ok": r.ok}
    if action == "list-vhosts":
        r = execute_command(
            server_id,
            "ls /etc/nginx/sites-enabled/ 2>/dev/null; "
            "ls /etc/nginx/conf.d/ 2>/dev/null; "
            "grep -r 'server_name' /etc/nginx/ 2>/dev/null | grep -v '#'",
        )
        return {"action": action, "output": r.stdout.strip()}
    if action == "status":
        r = execute_command(server_id, "systemctl status nginx 2>/dev/null || service nginx status 2>/dev/null")
        return {"action": action, "output": r.stdout.strip(), "ok": r.ok}
    r = execute_command(
        server_id,
        f"systemctl {action} nginx 2>/dev/null || service nginx {action} 2>/dev/null",
        use_sudo=esudo,
    )
    return {"action": action, "output": r.stdout.strip() or r.stderr.strip(), "ok": r.ok}


def apache_control(server_id: str, action: str, use_sudo: bool = True) -> dict:
    """Controls Apache (httpd/apache2) or inspects its configuration.

    action: status, reload, restart, stop, start, test, list-vhosts.
    """
    valid_actions = {"status", "reload", "restart", "stop", "start", "test", "list-vhosts"}
    if action not in valid_actions:
        raise ValueError(f"action must be one of: {valid_actions}")
    cfg = _get_config(server_id)
    esudo = use_sudo and not cfg.is_root
    svc = "$(command -v apache2 >/dev/null 2>&1 && echo apache2 || echo httpd)"
    if action == "test":
        r = execute_command(
            server_id,
            "apache2ctl -t 2>&1 || apachectl -t 2>&1 || httpd -t 2>&1",
            use_sudo=esudo,
        )
        return {"action": action, "output": r.stdout.strip() or r.stderr.strip(), "ok": r.ok}
    if action == "list-vhosts":
        r = execute_command(server_id, "apache2ctl -S 2>&1 || apachectl -S 2>&1 || httpd -S 2>&1")
        return {"action": action, "output": r.stdout.strip() or r.stderr.strip()}
    if action == "status":
        r = execute_command(
            server_id,
            f"systemctl status {svc} 2>/dev/null || service {svc} status 2>/dev/null",
        )
        return {"action": action, "output": r.stdout.strip(), "ok": r.ok}
    r = execute_command(
        server_id,
        f"systemctl {action} {svc} 2>/dev/null || service {svc} {action} 2>/dev/null",
        use_sudo=esudo,
    )
    return {"action": action, "output": r.stdout.strip() or r.stderr.strip(), "ok": r.ok}


# ──────────────────────────────────────────────────────────────────────────────
# Multi-server
# ──────────────────────────────────────────────────────────────────────────────

def broadcast_command(
    server_ids: list[str],
    command: str,
    use_sudo: bool = False,
    timeout: int = 60,
) -> list[dict]:
    """Runs the same shell command on multiple servers in parallel.

    Returns a list of result dicts keyed by server_id.
    """
    import concurrent.futures

    def _one(sid: str) -> dict:
        try:
            r = execute_command(sid, command, use_sudo=use_sudo, timeout=timeout)
            return {"server_id": sid, **r.as_dict()}
        except Exception as exc:  # noqa: BLE001
            return {"server_id": sid, "error": str(exc), "ok": False}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(server_ids))) as pool:
        return list(pool.map(_one, server_ids))


# ──────────────────────────────────────────────────────────────────────────────
# Server registry (public)
# ──────────────────────────────────────────────────────────────────────────────

def list_servers() -> list[dict]:
    """Returns the list of configured servers without credentials."""
    return [
        {
            "id": cfg.id,
            "label": cfg.label,
            "host": cfg.host,
            "port": cfg.port,
            "user": cfg.user,
            "auth": "key" if cfg.key_path else "password",
            "is_root": cfg.is_root,
        }
        for cfg in SERVERS.values()
    ]
