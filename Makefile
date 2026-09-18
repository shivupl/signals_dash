.PHONY: help up down logs test test-unit test-pg lint fmt typecheck layers migrate seed replay fixtures web psql redis shell

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n",$$1,$$2}'

up:         ## Start postgres, redis, worker and api
	docker compose up -d --build
down:       ## Stop everything
	docker compose down
logs:       ## Tail worker + api logs
	docker compose logs -f worker api

test:       ## Full suite (needs postgres up for -m pg tests)
	docker compose run --rm worker pytest -q
test-unit:  ## Pure tests only -- no Postgres, no network
	docker compose run --rm worker pytest -q -m "not pg"
test-pg:    ## Only the tests that need a real Postgres
	docker compose run --rm worker pytest -q -m pg

lint:       ## ruff check
	docker compose run --rm worker ruff check src tests scripts
fmt:        ## ruff format
	docker compose run --rm worker ruff format src tests scripts
typecheck:  ## mypy --strict
	docker compose run --rm worker mypy
layers:     ## Enforce the parsers/scoring purity contracts
	docker compose run --rm worker lint-imports

migrate:    ## Apply unapplied db/*.sql
	docker compose run --rm worker python -m signals migrate
seed:       ## Load company_tickers.json and mark the watchlist
	docker compose run --rm worker python -m signals seed
replay:     ## Populate the feed from captured fixtures (no network)
	docker compose run --rm worker python -m signals replay
web:        ## Build the React UI into web/dist
	cd web && npm install && npm run build
fixtures:   ## Re-capture test fixtures from the live network (run by hand)
	docker compose run --rm worker python scripts/capture_fixtures.py --allow-network

psql:       ## Open a psql shell
	docker compose exec postgres psql -U postgres -d signals
redis:      ## Open a redis-cli shell
	docker compose exec redis redis-cli
shell:      ## Bash inside the app image
	docker compose run --rm worker bash
