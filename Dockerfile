# syntax=docker/dockerfile:1
# Diligens in a container: the web app by default, the CLI and the MCP server on demand.
# Keys come from the environment at run time (compose reads .env); none are in the image.

FROM ghcr.io/astral-sh/uv:0.10.8 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --create-home --uid 1000 diligens \
    && install -d -o diligens -g diligens /app
WORKDIR /app
COPY --from=build --chown=diligens:diligens /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER diligens
# The synthetic demo company, so "Analysera demobolaget" works out of the box.
RUN diligens generate --out data/demo/demo_bolag.se \
    && mkdir -p data/private/arbetsyta output
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/', timeout=4)"
# Inside the container the app listens on every interface; compose publishes the port on
# the host's 127.0.0.1 only, so it stays reachable from this computer alone.
CMD ["diligens", "app", "--host", "0.0.0.0", "--inte-oppna"]
