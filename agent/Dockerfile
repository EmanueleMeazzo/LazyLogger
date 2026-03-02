FROM python:3.13-slim

# Install Node.js 22 (needed for MCP server subprocess)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install MCP server globally so npx doesn't re-download each time
RUN npm install -g @mauricio.wolff/mcp-obsidian

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml ./
RUN uv lock && uv sync --frozen --no-dev

# Copy application code
COPY src/ ./src/
COPY system_prompt.md ./

CMD ["uv", "run", "python", "-m", "src.main"]
