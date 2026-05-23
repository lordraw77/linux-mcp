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

# .env is mounted at runtime, never baked into the image
ENV PYTHONUNBUFFERED=1

# MCP servers communicate over stdio
ENTRYPOINT ["python", "server.py"]
