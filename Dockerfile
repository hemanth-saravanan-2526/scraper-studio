FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Combine package installs into a single RUN to reduce image layers
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    build-essential \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt && \
    python -m playwright install --with-deps chromium

COPY . .

EXPOSE 3682 3683 8931

# Use exec form for CMD so process signals reach the runner
CMD ["python", "-m", "uvicorn", "api_service:app", "--host", "0.0.0.0", "--port", "3683"]

