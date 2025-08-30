FROM python:3.11-slim

# Set work directory
WORKDIR /app

# Install necessary networking tools: ping, telnet, netstat, curl, wget
RUN apt-get update && \
    apt-get install -y iputils-ping telnet net-tools curl wget && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose the port your Flask app runs on
EXPOSE 5050

# Define environment variable for production
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0
ENV FLASK_RUN_PORT=5050

# Start the Flask app
CMD ["python3", "app.py"]

