.PHONY: install dev lint typecheck test demo serve worker docker-up docker-down clean

PY ?= .venv/bin/python
UV ?= uv

install:
	$(UV) venv .venv
	$(UV) pip install --python $(PY) -e ".[dev,postgres]"

lint:
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests

typecheck:
	$(PY) -m mypy

test:
	$(PY) -m pytest -q

check: lint typecheck test

demo:
	$(PY) -m zaheendata.cli demo

serve:
	$(PY) -m zaheendata.cli serve --reload

worker:
	$(PY) -m zaheendata.cli worker

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

clean:
	rm -rf data/*.db data/*.sqlite data/demo-shortlist.csv .pytest_cache .mypy_cache .ruff_cache
