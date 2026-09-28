ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
# Tag is in the ARG default and build.yaml; the Supervisor overrides BUILD_FROM
# per architecture, so it cannot be tagged inline.
# hadolint ignore=DL3006
FROM ${BUILD_FROM}

SHELL ["/bin/ash", "-o", "pipefail", "-c"]

ARG BUILD_ARCH=amd64

# The server is the Python standard library; nothing is pip-installed.
RUN apk add --no-cache python3 ca-certificates \
 && python3 -c "import http.server, ssl, urllib.request; print('python ok')"

WORKDIR /opt/agent_memory
COPY server.py store.py mcp.py filters.py namespaces.py page.py enrich.py embed.py ./

RUN python3 -m py_compile server.py store.py mcp.py filters.py namespaces.py page.py enrich.py embed.py

COPY run.sh /run.sh
RUN chmod +x /run.sh

ENV PYTHONUNBUFFERED=1

CMD ["/run.sh"]

ARG BUILD_DATE
ARG BUILD_DESCRIPTION
ARG BUILD_NAME
ARG BUILD_REF
ARG BUILD_REPOSITORY
ARG BUILD_VERSION
LABEL \
    io.hass.name="${BUILD_NAME}" \
    io.hass.description="${BUILD_DESCRIPTION}" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}" \
    maintainer="Lewis Heidrick" \
    org.opencontainers.image.title="${BUILD_NAME}" \
    org.opencontainers.image.description="${BUILD_DESCRIPTION}" \
    org.opencontainers.image.vendor="Lewis Heidrick" \
    org.opencontainers.image.authors="Lewis Heidrick" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.url="https://github.com/heidrickla/ha-agent_memory" \
    org.opencontainers.image.source="https://github.com/${BUILD_REPOSITORY}" \
    org.opencontainers.image.documentation="https://github.com/${BUILD_REPOSITORY}/blob/main/agent_memory/DOCS.md" \
    org.opencontainers.image.created="${BUILD_DATE}" \
    org.opencontainers.image.revision="${BUILD_REF}" \
    org.opencontainers.image.version="${BUILD_VERSION}"
