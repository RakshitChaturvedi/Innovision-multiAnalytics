.PHONY: infra migrate test health demo

COMPOSE_DEV := docker compose -f infra/docker-compose.dev.yml

infra:
	$(COMPOSE_DEV) up -d

migrate:
	alembic -c migrations/alembic.ini upgrade head

test:
	pytest -q

health:
	@echo "health: not implemented yet (planned: python tools/check_health.py)"

demo:
	@echo "demo: not implemented yet (planned: tools/mock_platform feeder + services + alert sink + viewer)"
