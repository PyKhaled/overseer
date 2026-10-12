SHELL := /bin/sh

.DEFAULT_GOAL := help

PYTHON ?= python3
VENV ?= .venv
VENV_PYTHON := $(VENV)/bin/python
IMAGE ?= overseer:local
SMOKE_CONTAINER ?= overseer-smoke
SMOKE_PORT ?= 8765

.PHONY: help setup check-env run serve docs-serve docs-build test coverage lint \
	format-check format audit check image smoke clean

help: ## Show available development commands.
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Create the virtual environment and install development dependencies.
	@$(PYTHON) -c 'import sys; sys.exit("Python 3.11 or newer is required (found %s)." % sys.version.split()[0]) if sys.version_info < (3, 11) else print("Using Python %s" % sys.version.split()[0])'
	$(PYTHON) -m venv $(VENV)
	$(VENV_PYTHON) -m pip install --upgrade "pip>=25.1"
	$(VENV_PYTHON) -m pip install --group dev
	$(VENV_PYTHON) -m pip uninstall --yes setuptools

check-env:
	@test -x "$(VENV_PYTHON)" || { printf '%s\n' 'Missing $(VENV_PYTHON). Run `make setup PYTHON=python3.14` first.' >&2; exit 1; }

run: check-env ## Run the Flask development server.
	$(VENV_PYTHON) -m overseer

serve: check-env ## Run the application locally with Gunicorn.
	$(VENV_PYTHON) -m gunicorn --bind 0.0.0.0:8765 --workers 2 overseer:app

docs-serve: check-env ## Preview the documentation website at http://127.0.0.1:8001.
	$(VENV_PYTHON) -m mkdocs serve --dev-addr 127.0.0.1:8001

docs-build: check-env ## Build the documentation website and fail on warnings.
	$(VENV_PYTHON) -m mkdocs build --strict

test: check-env ## Run the unit test suite.
	$(VENV_PYTHON) -m unittest discover -s tests -v
	node --test tests/*.cjs

coverage: check-env ## Run tests and enforce the configured coverage threshold.
	$(VENV_PYTHON) -m coverage run -m unittest discover -s tests -v
	$(VENV_PYTHON) -m coverage report
	node --test tests/*.cjs

lint: check-env ## Check the source with Ruff.
	$(VENV_PYTHON) -m ruff check .

format-check: check-env ## Verify Ruff formatting without changing files.
	$(VENV_PYTHON) -m ruff format --check .

format: check-env ## Apply Ruff lint fixes and formatting.
	$(VENV_PYTHON) -m ruff check --fix .
	$(VENV_PYTHON) -m ruff format .

audit: check-env ## Audit Python dependencies for known vulnerabilities.
	$(VENV_PYTHON) -m pip_audit

check: lint format-check coverage audit ## Run the complete local quality gate.

image: ## Build the local production image.
	docker build -t $(IMAGE) .

smoke: image ## Build and smoke-test the production image.
	@set -eu; \
	cleanup() { docker rm --force "$(SMOKE_CONTAINER)" >/dev/null 2>&1 || true; }; \
	trap cleanup EXIT INT TERM; \
	cleanup; \
	docker run --detach \
		--name "$(SMOKE_CONTAINER)" \
		--publish "127.0.0.1:$(SMOKE_PORT):8765" \
		--volume /var/run/docker.sock:/var/run/docker.sock \
		"$(IMAGE)" >/dev/null; \
	attempt=1; \
	while [ "$$attempt" -le 30 ]; do \
		if curl --fail --silent "http://127.0.0.1:$(SMOKE_PORT)/healthz" >/dev/null; then \
			curl --fail --silent "http://127.0.0.1:$(SMOKE_PORT)/" >/dev/null; \
			curl --fail --silent "http://127.0.0.1:$(SMOKE_PORT)/api/services" >/dev/null; \
			printf '%s\n' 'Smoke test passed.'; \
			exit 0; \
		fi; \
		attempt=$$((attempt + 1)); \
		sleep 1; \
	done; \
	docker logs "$(SMOKE_CONTAINER)"; \
	exit 1

clean: ## Remove generated caches and coverage reports.
	rm -rf .coverage coverage.xml htmlcov .mypy_cache .pytest_cache .ruff_cache
	find overseer tests -type d -name __pycache__ -prune -exec rm -rf {} +
