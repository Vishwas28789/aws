# Base image with Python 3.10
FROM python:3.10-slim

# Install system dependencies (git is needed for cloning repositories)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (needed for building frontend apps and node backends)
RUN curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y nodejs

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project
COPY . .

# Set environment variable for the port (Render will override this)
ENV PORT=8000

# Command to run the orchestrator
CMD ["python", "orchestrator/api.py"]
