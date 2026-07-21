FROM python:3.12-slim

# Install X11 client libraries and dependencies
RUN apt-get update && apt-get install -y \
    libx11-6 \
    libxext6 \
    libxrandr2 \
    libxfixes3 \
    libxinerama1 \
    libxcursor1 \
    libxi6 \
    libxtst6 \
    libgl1 \
    libglib2.0-0 \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Install computer-control-mcp
RUN pip install --no-cache-dir computer-control-mcp

# Expose MCP server port
EXPOSE 8080

# Run MCP server with STREAMABLE_HTTP transport
CMD ["python", "-c", "from computer_control_mcp.core import mcp; mcp.run(transport='http', host='0.0.0.0', port=8080)"]
