.PHONY: up down run test lint migrate seed
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