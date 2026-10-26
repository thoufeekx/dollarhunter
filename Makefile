DC=docker compose
.PHONY: up migrate seed test demo reset down logs
up:
	$(DC) up -d --build --wait db web
	$(DC) up -d worker
migrate:
	$(DC) run --rm web alembic upgrade head
seed:
	$(DC) run --rm web python -m app.seed
test:
	$(DC) run --rm -e DH_DATABASE_URL=postgresql+psycopg://dollarhunter:dollarhunter@db:5432/dollarhunter_test web pytest -q
demo:
	@echo "Open http://localhost:8000/demo (admin token required)"
reset:
	$(DC) run --rm web python -m app.seed --reset
down:
	$(DC) down
logs:
	$(DC) logs -f --tail=100
