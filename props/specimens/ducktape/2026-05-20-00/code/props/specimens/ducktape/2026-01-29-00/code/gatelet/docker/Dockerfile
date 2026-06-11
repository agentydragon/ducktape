FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    libpq-dev \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml ./
COPY gatelet/ ./gatelet/
COPY alembic.ini ./

# Install Python dependencies
RUN pip install --no-cache-dir -e '.[dev]'

# Create a non-root user
RUN useradd -m -u 1000 gatelet && chown -R gatelet:gatelet /app
USER gatelet

# Copy entrypoint script
COPY --chown=gatelet:gatelet docker/entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

# Expose port
EXPOSE 8000

# Set entrypoint
ENTRYPOINT ["/app/entrypoint.sh"]

# Default command (can be overridden)
CMD ["uvicorn", "gatelet.server.app:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
