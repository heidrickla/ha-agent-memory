# The base image is multi-architecture; the build's platform picks amd64 or aarch64.
# Alpine 3.21 carries the python3 (3.12, with SQLite FTS5) the tests run on.
FROM ghcr.io/home-assistant/base:3.21

SHELL ["/bin/ash", "-o", "pipefail", "-c"]

# The server is the Python standard library; nothing is pip-installed. The base tag fixes the
# Alpine branch; an exact apk pin breaks the rebuild once Alpine replaces that version within it.
# hadolint ignore=DL3018
RUN apk add --no-cache python3 ca-certificates \
 && python3 -c "import http.server, sqlite3, ssl, urllib.request; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)'); print('python ok')"

WORKDIR /opt/agent_memory
COPY server.py store.py mcp.py filters.py namespaces.py page.py enrich.py embed.py review.py ./
COPY page.css ./

RUN python3 -m py_compile server.py store.py mcp.py filters.py namespaces.py page.py enrich.py embed.py review.py

COPY run.sh /run.sh
RUN chmod +x /run.sh

ENV PYTHONUNBUFFERED=1

# The Supervisor restarts the add-on when this fails.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=5)"]

CMD ["/run.sh"]

ARG BUILD_ARCH=amd64
ARG BUILD_VERSION
LABEL \
    io.hass.name="Agent Memory" \
    io.hass.description="Shared, bounded memory for coding agents over MCP" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    maintainer="Lewis Heidrick" \
    org.opencontainers.image.title="Agent Memory" \
    org.opencontainers.image.description="Shared, bounded memory for coding agents over MCP" \
    org.opencontainers.image.vendor="Lewis Heidrick" \
    org.opencontainers.image.authors="Lewis Heidrick" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.url="https://github.com/heidrickla/ha-agent-memory" \
    org.opencontainers.image.source="https://github.com/heidrickla/ha-agent-memory" \
    org.opencontainers.image.documentation="https://github.com/heidrickla/ha-agent-memory/blob/main/agent_memory/DOCS.md" \
    org.opencontainers.image.version="${BUILD_VERSION}"
