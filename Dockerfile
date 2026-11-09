# Self-contained gateway image. Stateless apart from the key file and (in-memory) quotas.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GATEWAY_USAGE_DIR=/var/lib/gateway \
    GATEWAY_LOG_STDOUT=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY api_proxy.py usage_report.py access_log.sh ./
RUN useradd -u 10001 -m gateway && mkdir -p /var/lib/gateway && chown -R gateway /var/lib/gateway
USER 10001
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
# Prometheus metrics, and only listening when GATEWAY_METRICS_PORT is set.
# Nothing proxies it: the app port is the one an ingress ever fronts.
EXPOSE 9091
# liveness: the only unauthenticated endpoint
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).status==200 else 1)"

ENTRYPOINT ["python", "api_proxy.py"]
CMD ["--port", "8000", "--host", "0.0.0.0"]
