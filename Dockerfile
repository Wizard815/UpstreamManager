# Upstream Manager in a container. It works on git repos that live on the host and (optionally) talks
# to herdr over its socket, so docker-compose.yml mounts those at the same paths they have on the host.
# Docker Hub rate-limited? Build from a mirror: PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.13-slim-bookworm
ARG PYTHON_IMAGE=python:3.13-slim-bookworm
FROM ${PYTHON_IMAGE}

RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv

WORKDIR /app
ENV UV_PYTHON=3.13 UV_PROJECT_ENVIRONMENT=/app/.venv UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# Runs as whatever user compose names (your own uid), so files it writes on the host are yours.
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 HOST=127.0.0.1 PORT=5001 \
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=*
EXPOSE 5001
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/llm/status' % os.environ.get('PORT','5001'), timeout=4)" || exit 1
CMD ["upstream-manager"]
