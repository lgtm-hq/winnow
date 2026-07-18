# syntax=docker/dockerfile:1
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
FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc \
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
FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

LABEL org.opencontainers.image.description="Winnow — organize and deduplicate your media library."

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

RUN useradd --create-home --no-log-init --uid 1001 --user-group winnow && \
    chown -R winnow:winnow /app

USER winnow

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD winnow --version || exit 1

ENTRYPOINT ["winnow"]
CMD ["--help"]
