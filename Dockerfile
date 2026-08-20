FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY frontend ./frontend
COPY skills ./skills
COPY config ./config
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh
RUN pip install --no-cache-dir .
RUN chmod +x /app/scripts/docker-entrypoint.sh \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser
EXPOSE 8100
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["ops-agent-api"]
