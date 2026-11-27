PY ?= python3.13
VENV ?= .venv
BIN := $(VENV)/bin
TEST_DATABASE_URL ?= postgresql+psycopg://aitrace:aitrace@127.0.0.1:5432/aitrace_test

.PHONY: install install-mock dev mock test test-unit test-integration contract e2e lint migrate run verify perf

install:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -e ".[dev]"

install-mock:
	$(BIN)/pip install -e mocks/aitest

dev:
	$(BIN)/uvicorn aitrace.api.app:create_app --factory --reload --host 127.0.0.1 --port 8080

mock:
	$(BIN)/uvicorn aitest_mock.main:app --host 127.0.0.1 --port 8090

test:
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(BIN)/pytest --cov=aitrace --cov-fail-under=85

test-unit:
	$(BIN)/pytest tests/unit

test-integration:
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(BIN)/pytest tests/integration

contract:
	$(BIN)/python tools/export_contracts.py --check
	$(BIN)/pytest tests/contract

e2e:
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(BIN)/pytest tests/e2e

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy

migrate:
	AITRACE_DATABASE__DSN=$(TEST_DATABASE_URL) $(BIN)/alembic upgrade head

run:
	$(BIN)/aitrace-server

verify: lint test contract e2e

perf:
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(BIN)/python tools/perf_harness.py --profile all
