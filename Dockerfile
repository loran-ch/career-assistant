FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt pyproject.toml LICENSE README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip \
    && python -m pip install . \
    && python -m playwright install --with-deps chromium \
    && chmod -R 755 /ms-playwright

COPY main.py config.example.yaml ./
RUN useradd --create-home --uid 10001 career \
    && mkdir -p /app/data \
    && chown -R career:career /app

USER career
EXPOSE 5000

ENTRYPOINT ["python", "main.py"]
