.PHONY: install dev test lint fmt doctor cycle dashboard web story backtest mcp clean

install:
	python3 -m pip install -e .

dev:
	python3 -m pip install -e ".[dev]"

test:
	python3 -m pytest -q

lint:
	python3 -m ruff check src tests

fmt:
	python3 -m ruff check --fix src tests
	python3 -m ruff format src tests

doctor:
	desk doctor

cycle:
	desk run-cycle --phase morning --dry-run

dashboard:
	desk dashboard

web:
	desk dashboard --web

story:
	desk story

backtest:
	desk backtest

mcp:
	desk mcp-server

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
