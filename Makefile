.PHONY: up down run test lint migrate seed serve scheduler worker sandbox bench web-install web web-check web-build
up:
	docker compose up -d
down:
	docker compose down
run:
	uvicorn services.api.main:app --reload --port 8000
test:
	pytest -q
lint:
	ruff check .
migrate:
	python -m scripts.migrate
seed:
	python -m scripts.seed_agents
serve:
	uvicorn services.api.main:app --port 8000
scheduler:
	python -m services.scheduler
worker:
	python -m services.worker
sandbox:
	docker pull python:3.12-slim
bench:
	python -m scripts.benchmark
web-install:
	npm --prefix frontend ci
web:
	npm --prefix frontend run dev
web-check:
	npm --prefix frontend run typecheck
	npm --prefix frontend run lint
	npm --prefix frontend test
web-build:
	npm --prefix frontend run build
