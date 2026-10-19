FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends nginx openssl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE /app/
COPY qdrant_memory /app/qdrant_memory
ARG MCP_REQUIREMENT=mcp>=1.30,<2
RUN pip install --no-cache-dir '.[remote]' 'pytest>=8,<10' "$MCP_REQUIREMENT"
COPY tests/__init__.py /app/tests/__init__.py
COPY tests/helpers.py /app/tests/helpers.py
COPY tests/remote /app/tests/remote
ENTRYPOINT ["python", "tests/remote/fixture.py"]
