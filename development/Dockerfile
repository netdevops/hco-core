FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VIRTUALENVS_CREATE=false

RUN pip install --no-cache-dir poetry==2.3.2

WORKDIR /app
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root --no-interaction
COPY hco_core ./hco_core
COPY tests/fixtures ./tests/fixtures
RUN poetry install --only main --no-interaction

RUN useradd --create-home hco
USER hco
EXPOSE 8080
CMD ["hco-core", "serve", "api", "--host", "0.0.0.0", "--port", "8080"]
