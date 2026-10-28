# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
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
FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff \
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
FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

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
