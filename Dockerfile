# syntax=docker/dockerfile:1
FROM node:22.20.0-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM registry.gitlab.com/gitlab-org/cli:v1.95.0 AS glab
FROM python:3.12.12-slim-bookworm

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=glab /usr/bin/glab /usr/local/bin/glab
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
      cp /run/secrets/proxy_ca /usr/local/share/ca-certificates/build-proxy.crt; \
      update-ca-certificates; \
    fi \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends git ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt npm install --global @qwen-code/qwen-code@0.26.1

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_SYSTEM_CERTS=true PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" WORKSPACE_ROOT=/workspace DATA_ROOT=/data \
    LANGGRAPH_URL=http://127.0.0.1:2024 LANGSMITH_TRACING=false \
    LANGGRAPH_CLI_NO_ANALYTICS=1 NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY prompts ./prompts
COPY config ./config
COPY skills ./skills
RUN uv sync --frozen --no-dev
COPY langgraph.json graph.py ./
RUN mkdir -p /workspace /data/langgraph \
    && ln -s /data/langgraph /app/.langgraph_api
VOLUME ["/workspace", "/data"]
EXPOSE 2024
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=5 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:2024/ok', headers={'x-api-key':os.environ.get('ORCHESTRATOR_TOKEN','')}), timeout=3)"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["langgraph", "dev", "--host", "0.0.0.0", "--port", "2024", "--no-browser", "--no-reload", "--n-jobs-per-worker", "10"]
