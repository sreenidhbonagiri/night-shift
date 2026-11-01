#!/bin/sh
# Night Shift execution backend entrypoint.
# Starts Ollama in the background, pulls the model on first boot,
# then serves the FastAPI app under uvicorn.
set -e

echo "[entrypoint] starting Ollama..."
ollama serve &
OLLAMA_PID=$!

# Wait for Ollama to be ready.
echo "[entrypoint] waiting for Ollama to be ready..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
        echo "[entrypoint] Ollama is ready."
        break
    fi
    sleep 1
done

# Pull the model if not already present.
echo "[entrypoint] ensuring model $OLLAMA_MODEL is available..."
ollama pull "$OLLAMA_MODEL" || {
    echo "[entrypoint] WARNING: model pull failed; inference will be unavailable."
    echo "[entrypoint] The app will start and report honest errors on model calls."
}

echo "[entrypoint] starting uvicorn on :8080..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8080
