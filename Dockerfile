# Use the official Playwright Python base image with pre-installed browser system dependencies
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Set working directory inside the container
WORKDIR /app

# Copy requirements file first to leverage Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the backend application code
COPY . .

# Download and install the exact matched version of Chromium for Playwright
RUN playwright install chromium

# Make start.sh executable
RUN chmod +x start.sh

# Expose port (FastAPI default is 8000, overridden by Render via $PORT environment variable)
EXPOSE 8000

# Default command to run (can be overridden by Render via dockerCommand)
CMD ["./start.sh"]
