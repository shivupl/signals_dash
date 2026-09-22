# Deploying

One small VPS running `docker-compose.prod.yml`: Postgres, Redis, the worker, the
API, and Caddy for automatic HTTPS. Only ports 80 and 443 are published.

## First time

On the server, as a non-root user in the `docker` group:

    git clone https://github.com/shivupl/signals_dash.git signals && cd signals

From your machine, copy the two files git never carries:

    scp .env                  signals@HOST:signals/.env
    scp config/watchlist.yml  signals@HOST:signals/config/watchlist.yml

Then add to the server's `.env`: `SIGNALS_DOMAIN`, and a strong
`POSTGRES_PASSWORD` with a `DATABASE_URL` that matches it. Then:

    docker compose -f docker-compose.prod.yml up -d --build
    docker compose -f docker-compose.prod.yml run --rm worker python -m signals migrate
    docker compose -f docker-compose.prod.yml run --rm worker python -m signals seed

The domain's A record must point at the server before Caddy starts, or the
certificate request fails and Caddy retries with backoff.

## Carrying data over

    docker compose exec -T postgres pg_dump -U postgres -Fc signals > signals.dump
    scp signals.dump signals@HOST:
    # on the server, with the worker stopped:
    docker compose -f docker-compose.prod.yml exec -T postgres \
        pg_restore -U postgres -d signals --clean --if-exists --no-owner < ~/signals.dump

## Updating

    git pull && docker compose -f docker-compose.prod.yml up -d --build
    docker compose -f docker-compose.prod.yml run --rm worker python -m signals migrate

Every service is `restart: unless-stopped`, so the stack comes back after a reboot.
