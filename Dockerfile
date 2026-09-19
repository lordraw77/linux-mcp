FROM python:3.12-slim

LABEL org.opencontainers.image.title="linux-ssh-mcp" \
      org.opencontainers.image.description="MCP server with 48 SSH tools for managing remote Linux servers" \
      org.opencontainers.image.source="https://github.com/lordraw77/linux-mcp" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

# System deps: openssh-client for known_hosts discovery, openssl for cert checks
RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
        openssl \
        iputils-ping \
        traceroute \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ssh_manager.py ./

ENV PYTHONUNBUFFERED=1

# Transport mode: stdio (default) | streamable-http | sse (deprecated)
# Override with -e UXMCP_TRANSPORT=streamable-http at runtime.
ENV UXMCP_TRANSPORT=stdio
# Host/port default to 0.0.0.0:8080 (UXMCP_HTTP_HOST / UXMCP_HTTP_PORT)

# Expose HTTP port (only used when UXMCP_TRANSPORT=streamable-http or sse)
EXPOSE 8080

# .env is mounted at runtime, never baked into the image
ENTRYPOINT ["python", "server.py"]
