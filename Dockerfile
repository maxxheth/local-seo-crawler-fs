FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:/root/.local/bin:$PATH"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Python dependencies
COPY pyproject.toml README.md .
COPY src/ src/
RUN uv venv .venv && \
    . .venv/bin/activate && \
    uv pip install -e .

COPY . .

CMD ["local-seo-crawler", "--help"]
