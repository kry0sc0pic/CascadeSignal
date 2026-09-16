.PHONY: help setup sync lint format typecheck test check dvc-status dvc-repro

help:
	@printf "CascadeSignal setup targets:\n"
	@printf "  setup       Create .venv, sync dependencies, install pre-commit\n"
	@printf "  sync        Sync uv dependencies\n"
	@printf "  lint        Run ruff lint checks\n"
	@printf "  format      Format Python files and apply safe lint fixes\n"
	@printf "  typecheck   Run mypy\n"
	@printf "  test        Run pytest\n"
	@printf "  check       Run format check, lint, typecheck, tests, and DVC status\n"

setup:
	uv venv --python 3.12
	uv sync --all-groups
	uv run pre-commit install

sync:
	uv sync --all-groups

lint:
	uv run ruff check .

format:
	uv run black .
	uv run ruff check --fix .

typecheck:
	uv run mypy src tests scripts

test:
	uv run pytest

check:
	uv run black --check .
	uv run ruff check .
	uv run mypy src tests scripts
	uv run pytest
