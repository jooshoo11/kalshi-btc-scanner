# Dockerfile — Kalshi BTC/ETH 15M Binary Options Trading Bot
# Multi-stage: build deps, then slim runtime
FROM python:3.11-slim AS builder

WORKDIR /app

# System deps for cryptography, websockets, numpy
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Runtime stage
FROM python:3.11-slim

WORKDIR /app

# Non-root user for security
RUN useradd -m -s /bin/bash botuser

# Copy installed packages from builder
COPY --from=builder /root/.local /home/botuser/.local
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# Copy application source
COPY core/ ./core/
COPY coins/ ./coins/
COPY app.py run_live_bot.py paper_trader.py kalshi_btc_scanner.py btc_metrics.py ./
COPY schemas/ ./schemas/
COPY scripts/ ./scripts/
COPY backtests/ ./backtests/
COPY .env.example .env.example

# Runtime data directories (volumes mounted at runtime)
RUN mkdir -p data/ticks data/backtest_validation coins/btc/logs coins/eth/logs \
    && chown -R botuser:botuser /app

USER botuser

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PATH="/home/botuser/.local/bin:${PATH}"

# Dashboard port
EXPOSE 8088

# Default: paper mode, headless. Override with --serve or --mode live via docker compose.
CMD ["python3", "run_live_bot.py", "--mode", "paper"]
