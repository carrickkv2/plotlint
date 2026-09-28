FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY farm_list_check ./farm_list_check
COPY sql ./sql
RUN pip install --no-cache-dir .
