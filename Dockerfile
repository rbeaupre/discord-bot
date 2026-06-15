# ── Build stage ───────────────────────────────────────────────────────────────
# python:3.13-slim is a minimal Debian image with Python pre-installed.
# It's much smaller than the full python:3.13 image (~50 MB vs ~1 GB).
FROM python:3.13-slim

# Set the working directory inside the container.
WORKDIR /app

# Copy only the requirements file first so Docker can cache the pip install
# layer independently from the application code. This means a code change
# won't trigger a full pip reinstall on every build.
COPY requirements.txt .

# Install Python dependencies.
# --no-cache-dir keeps the image smaller by not storing the pip download cache.
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container.
# Files listed in .dockerignore are excluded (e.g. .venv, .env, __pycache__).
COPY . .

# Create a non-root user and switch to it for security.
# Running as root in a container is a bad practice — if the container is
# ever compromised, the attacker would have root inside the container.
RUN useradd --create-home botuser && chown -R botuser:botuser /app
USER botuser

# Start the bot. Secrets are injected at runtime via environment variables
# (set in docker-compose.yml locally, or via GCP Secret Manager / VM metadata
# in production) — never baked into the image.
CMD ["python", "bot.py"]
