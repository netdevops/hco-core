FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    VIRTUAL_ENV=/opt/hco
RUN pip install --no-cache-dir poetry==2.3.2 \
    && python -m venv /opt/hco
ENV PATH=/opt/hco/bin:$PATH

WORKDIR /app
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root --no-interaction
COPY hco_core ./hco_core
RUN poetry build --format wheel --no-interaction \
    && pip install --no-cache-dir --no-deps dist/*.whl

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/hco/bin:$PATH
COPY --from=builder /opt/hco /opt/hco
WORKDIR /app
COPY tests/fixtures ./tests/fixtures

RUN useradd --create-home hco
USER hco
EXPOSE 8080
CMD ["hco-core", "serve", "api", "--host", "0.0.0.0", "--port", "8080"]
