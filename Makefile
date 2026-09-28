.PHONY: install run migrate up down test lint typecheck eval check

install:
	uv sync --extra dev --extra embeddings

run:
	uv run python api.py

migrate:
	uv run python migrate.py

up:
	docker compose up --build

down:
	docker compose down

test:
	uv run pytest --cov --cov-report=term-missing

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

eval:
	uv run python -m evals.evaluate

check: lint typecheck test
