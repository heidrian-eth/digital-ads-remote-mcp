FROM python:3.10-slim

WORKDIR /app

# Copy submodule and wrapper
COPY google-ads-mcp/ ./google-ads-mcp/
COPY remote_server.py .
COPY worker.py .
COPY pyproject.toml .

# Install dependencies
RUN pip install --no-cache-dir -e ./google-ads-mcp && \
    pip install --no-cache-dir .

# Default port for container platforms
EXPOSE 8080

# Run the remote server
CMD ["python", "remote_server.py"]
