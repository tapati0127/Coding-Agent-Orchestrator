# syntax=docker/dockerfile:1
ARG GLAB_VERSION=latest
FROM gitlab/glab:${GLAB_VERSION} AS gitlab_cli
FROM node:22-bookworm-slim AS node
FROM python:3.13-slim-bookworm AS cli
COPY --from=gitlab_cli /usr/bin/glab /usr/local/bin/glab
RUN glab --version
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG QWEN_CODE_VERSION=latest
ARG OPENCODE_VERSION=latest
RUN --mount=type=secret,id=ca_bundle \
    if [ -f /run/secrets/ca_bundle ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca_bundle; fi; \
    npm install -g "@qwen-code/qwen-code@${QWEN_CODE_VERSION}" "opencode-ai@${OPENCODE_VERSION}" \
    && qwen --version && opencode --version
COPY requirements.txt /opt/coding-tools/requirements.txt
RUN --mount=type=secret,id=ca_bundle \
    if [ -f /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi; \
    pip install --no-cache-dir -r /opt/coding-tools/requirements.txt
ARG CODER_UID=1000
ARG CODER_GID=1000
RUN groupadd --gid ${CODER_GID} coder && useradd --uid ${CODER_UID} --gid ${CODER_GID} --create-home coder
USER coder
WORKDIR /home/coder
CMD ["qwen", "--version"]

# One application container contains the API and both coding CLIs.
FROM cli AS team
USER root
RUN --mount=type=secret,id=ca_bundle \
    if [ -f /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi; \
    pip install --no-cache-dir "langgraph-cli[inmem]>=0.4,<0.5"
WORKDIR /app
COPY --chown=coder:coder . /app
RUN mkdir -p /app/.langgraph_api && chown coder:coder /app/.langgraph_api
ENV CODING_CLI_RUNTIME=local WORKSPACE_ROOT=/workspace
USER coder
EXPOSE 2026
CMD ["langgraph", "dev", "--config", "/app/langgraph.docker.json", "--host", "0.0.0.0", "--port", "2026", "--no-browser"]
