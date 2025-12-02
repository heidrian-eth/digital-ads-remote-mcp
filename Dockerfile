FROM python:3.12-slim

WORKDIR /app

# Install pipx
RUN pip install --no-cache-dir pipx && \
    pipx ensurepath

# Copy submodules
COPY google-ads-mcp/ ./google-ads-mcp/
COPY google-analytics-mcp/ ./google-analytics-mcp/
COPY facebook-ads-mcp/ ./facebook-ads-mcp/

# Install submodules via pipx (uses audited commits, not PyPI)
RUN pipx install ./google-ads-mcp && \
    pipx install ./google-analytics-mcp

# Install Facebook Ads MCP dependencies (no pyproject.toml, uses requirements.txt)
RUN pip install --no-cache-dir -r ./facebook-ads-mcp/requirements.txt

# Copy and install wrapper
COPY remote_server.py .
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Add pipx bin to PATH
ENV PATH="/root/.local/bin:$PATH"

# Default port for container platforms
EXPOSE 8080

# Run the remote server
CMD ["python", "remote_server.py"]
