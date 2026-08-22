COMPOSE := docker compose -f grafana/docker-compose.yaml

.PHONY: help install test up down restart restart-grafana logs e2e traffic clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## install the package with dev extras
	pip install -e '.[dev]'

test: ## run unit tests
	pytest -q

up: ## build and start the full stack
	$(COMPOSE) up -d --build
	@echo
	@echo "app     http://localhost:$$(grep '^APP_PORT' grafana/.env | cut -d= -f2)"
	@echo "scalar  http://localhost:$$(grep '^APP_PORT' grafana/.env | cut -d= -f2)/scalar"
	@echo "grafana http://localhost:$$(grep '^GRAFANA_PORT' grafana/.env | cut -d= -f2)/d/wan"

down: ## stop the stack and delete its volumes
	$(COMPOSE) down -v

restart-grafana: ## restart only Grafana, to pick up provisioning or SENTRY_AUTH_TOKEN
	$(COMPOSE) up -d --force-recreate grafana

restart: ## rebuild and restart only the app
	$(COMPOSE) up -d --build app

logs: ## follow the app's JSON logs
	$(COMPOSE) logs -f app --no-log-prefix

e2e: ## end-to-end verification against a running stack
	python3 scripts/e2e.py

two-services: ## run app A -> app B and verify they share one correlation id
	$(COMPOSE) --profile two-services up -d --build service-a service-b
	python3 scripts/two_services.py

links: ## print Grafana deep links for the newest trace
	python3 scripts/links.py

traffic: ## generate some requests to look at in Grafana
	@port=$$(grep '^APP_PORT' grafana/.env | cut -d= -f2); \
	for i in $$(seq 1 30); do \
		curl -s -o /dev/null "http://localhost:$$port/random?minimum=0.02&maximum=0.3"; \
		[ $$((i % 5)) -eq 0 ] && curl -s -o /dev/null "http://localhost:$$port/nested"; \
		[ $$((i % 7)) -eq 0 ] && curl -s -o /dev/null "http://localhost:$$port/chain?depth=2"; \
		[ $$((i % 9)) -eq 0 ] && curl -s -o /dev/null "http://localhost:$$port/boom"; \
	done; echo "done"

clean: ## remove build artefacts
	rm -rf build dist *.egg-info .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
