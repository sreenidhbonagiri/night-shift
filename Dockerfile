# Night Shift execution backend for Fly.io.
# Runs the existing FastAPI app under uvicorn with python3, pip, git
# available for disposable venvs, QA subprocesses, and Git fixtures.
# Ollama is co-located on the same VM (qwen3:4b pulled at build time).
FROM flyio/flyctl:latest AS fly

# Python base with build tools for venv/pip in disposable QA environments.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        zstd \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Ollama for co-located inference (qwen3:4b).
RUN curl -fsSL https://ollama.com/install.sh | sh

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV OLLAMA_URL=http://localhost:11434/api/generate
ENV OLLAMA_MODEL=qwen3:4b
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Start Ollama, pull the model on first boot, then serve the app.
# The model pull (~2.5 GB) runs at runtime, not build time, to avoid
# a huge image layer. A startup script handles the pull-and-serve.
COPY deploy-entrypoint.sh /deploy-entrypoint.sh
RUN chmod +x /deploy-entrypoint.sh
CMD ["/deploy-entrypoint.sh"]
