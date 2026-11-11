# vault-watcher image (plan D3). Build context: the repository root.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY vault-core/pyproject.toml vault-core/
COPY vault-mcp/pyproject.toml vault-mcp/
COPY vault-watcher/pyproject.toml vault-watcher/
RUN uv sync --locked --no-dev --package vault-watcher --no-install-workspace
COPY vault-core vault-core
COPY vault-watcher vault-watcher
RUN uv sync --locked --no-dev --package vault-watcher --no-editable

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    VAULT_DIR=/vault \
    DATA_DIR=/data \
    POLICY_FILE=/config/vault-mcp.yaml
USER 1000:1000
ENTRYPOINT ["vault-watcher"]
CMD ["run"]
