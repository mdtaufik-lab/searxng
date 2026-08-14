.DEFAULT_GOAL := help
SHELL := /bin/bash

# Run these from inside the gateway/ directory.
# The SearXNG backend lives one level up (the repo root).
SEARXNG_ROOT := ..
SETTINGS := gateway/deploy/settings-local.yml

.PHONY: help
help:
	@echo "make searxng  - run the SearXNG backend  (127.0.0.1:8888, private)"
	@echo "make gateway  - run the API gateway      (127.0.0.1:8080, your API)"
	@echo "make test     - run the gateway test suite"
	@echo "make install  - (re)create the gateway virtualenv"

# One-time: create the gateway venv. (SearXNG's own venv is set up separately —
# see README 'Deploying to a VPS'.)
.PHONY: install
install:
	uv venv --python 3.13 .venv
	uv pip install --python .venv/bin/python -e ".[dev]"
	@test -f .env || cp .env.example .env

.PHONY: searxng
searxng:
	cd $(SEARXNG_ROOT) && SEARXNG_SETTINGS_PATH=$(SETTINGS) .venv/bin/python -m searx.webapp

.PHONY: gateway
gateway:
	.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload

.PHONY: test
test:
	.venv/bin/python -m pytest -q
