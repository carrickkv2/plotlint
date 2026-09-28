FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY plotlint ./plotlint
COPY sql ./sql
# Editable install keeps the package in /app, next to sql/, which the app reads on startup.
RUN pip install --no-cache-dir -e .
# Railway (and similar hosts) set $PORT; locally it defaults to 8000.
CMD ["sh", "-c", "uvicorn plotlint.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
