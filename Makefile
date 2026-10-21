PY ?= python3
RUFF ?= uvx ruff

.DEFAULT_GOAL := help

.PHONY: help test lint check build install smoke clean

help:  ## Show available targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

test:  ## Run the test suite
	$(PY) -m unittest discover -s tests -v

lint:  ## Lint with ruff
	$(RUFF) check src tests

check: lint test  ## Lint and test

build:  ## Build a wheel and verify its contents
	uv build --wheel
	$(PY) scripts/check_wheel.py dist/*.whl

install:  ## Install the CLI non-editable
	bash install.sh

smoke:  ## Smoke-test the installed command without a model call
	prmpt --version
	prmpt "smoke test" --print-prompt | grep -q "TERMINAL RUN"
	@echo "smoke ok"

clean:  ## Remove build artifacts and bytecode
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
	rm -rf build dist src/*.egg-info
