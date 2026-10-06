FROM python:3.12-slim

# git is needed to install the gitdb-py dependency from GitHub.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY pixel ./pixel
RUN pip install --no-cache-dir . \
    && useradd --system --no-create-home pixel

USER pixel
ENV PORT=8000
EXPOSE 8000

# A single worker: sessions are kept in memory and writes are serialised in-process.
CMD ["sh", "-c", "exec uvicorn pixel.app:app --host 0.0.0.0 --port ${PORT} --workers 1"]
