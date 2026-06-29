# OpenVC investor scraper — light (no browser; Cloudflare bypass for the LIST
# phase is via the external FlareSolverr/byparr container, the DETAIL phase uses
# tls-client + residential proxies). Postgres-only: list -> detail -> formate.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY . .

# Default = one full Postgres pipeline pass (list -> detail -> formate) and exit.
# docker-compose wraps this in a nightly loop.
CMD ["python", "-m", "src.scraper", "all-pg"]
