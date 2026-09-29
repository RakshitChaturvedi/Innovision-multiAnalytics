.PHONY: infra migrate test health

# Compose reads variables from --env-file; without it, compose looks for .env
# next to the compose file (infra/), not in the repo root, and silently falls
# back to its defaults.
COMPOSE_DEV := docker compose --env-file .env -f infra/docker-compose.dev.yml

.env:
	cp .env.example .env
	@echo "created .env from .env.example"

infra: .env
	$(COMPOSE_DEV) up -d

migrate:
	alembic -c migrations/alembic.ini upgrade head

test:
	pytest -q

health:
	@echo "health: not implemented yet (planned: python tools/check_health.py)"
