.PHONY: all setup install test test-integration test-all lint fmt clean help

-include .env

all: setup test

setup:
	@echo "Setting up development environment with uv..."
	uv sync --dev
	@echo "Setup complete. Try 'make test' or 'make lint'."

install: setup

test:
	@echo "Running unit tests with coverage (integration tests excluded by default locally, always run in CI)..."
	uv run pytest --cov=winnow --cov-report=term-missing --cov-report=xml --cov-report=html
	@echo "Coverage reports: htmlcov/index.html, coverage.xml"

test-integration:
	@echo "Running integration tests..."
	uv run pytest -m integration

# test-all runs unit + integration tests locally, i.e. what CI always runs.
test-all: test test-integration

lint:
	@echo "Running lintro check..."
	uv run lintro chk

fmt:
	@echo "Running lintro format..."
	uv run lintro fmt

clean:
	@echo "Cleaning build artifacts..."
	rm -rf build/ dist/ *.egg-info htmlcov/ .coverage coverage.xml .pytest_cache/ .mypy_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

help:
	@echo "Targets: all, setup, install, test, test-integration, test-all, lint, fmt, clean"
