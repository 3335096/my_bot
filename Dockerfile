FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY bot ./bot

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

CMD ["python", "-m", "bot.main"]
