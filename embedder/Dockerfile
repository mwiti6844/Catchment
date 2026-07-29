# syntax=docker/dockerfile:1

# BGE-M3 lives in its own image. Torch is installed from the CPU-only index:
# Docker Desktop gives a Linux container no GPU passthrough on macOS, so the
# CUDA wheels would be several GB of dead weight.
FROM python:3.12-slim-bookworm

RUN groupadd --system --gid 10002 embedder && \
    useradd --system --uid 10002 --gid embedder --no-create-home embedder

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

COPY --chown=embedder:embedder app.py ./

# Model weights land here; compose mounts a volume so they survive a rebuild
# rather than re-downloading ~2GB every time.
ENV HF_HOME=/models \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN mkdir -p /models && chown embedder:embedder /models

USER embedder
EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=5 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=3).status == 200 else 1)"]

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
