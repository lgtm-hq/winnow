# syntax=docker/dockerfile:1@sha256:572cebbc2b15e6f84c077742952674889cb63cd5e9b80abdffcbe587b6f19183
# SPDX-License-Identifier: MIT
# For license details, see the repository root LICENSE file.
# =============================================================================
# Winnow Docker Image (multi-stage)
# =============================================================================
# Stage `builder`: resolve and install dependencies into a virtualenv with uv
# Stage `runtime`: minimal Python runtime, non-root user, entrypoint winnow
# =============================================================================

# -----------------------------------------------------------------------------
# Stage: builder — install deps into .venv with uv
# -----------------------------------------------------------------------------
# Renovate manages digest bumps for pinned base images.
FROM python:3.15-slim@sha256:1c670c3568107e243bfd05457ad9ca0b11c35a855c985af73e4452db9a714e70 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d \
    /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY winnow/ ./winnow/

RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --no-editable

# -----------------------------------------------------------------------------
# Stage: runtime — slim Python runtime, non-root, entrypoint winnow
# -----------------------------------------------------------------------------
FROM python:3.15-slim@sha256:1c670c3568107e243bfd05457ad9ca0b11c35a855c985af73e4452db9a714e70

LABEL org.opencontainers.image.description="Winnow — organize and deduplicate your media library."

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

# The runtime never installs packages: drop the base image's pip (system
# interpreter, not the venv) so its vendored urllib3/msgpack/pkg_resources
# copies do not ship in the image.
RUN /usr/local/bin/python -m pip uninstall --yes --root-user-action=ignore pip && \
    useradd --create-home --no-log-init --uid 1001 --user-group winnow && \
    chown -R winnow:winnow /app

USER winnow

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD winnow --version || exit 1

ENTRYPOINT ["winnow"]
CMD ["--help"]
