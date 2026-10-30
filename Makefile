.PHONY: up down run test lint
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
