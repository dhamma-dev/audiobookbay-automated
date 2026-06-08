# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Install Tor so the app can route AudiobookBay requests through it. The app
# starts and manages the tor process itself, so no system service is needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tor \
    && rm -rf /var/lib/apt/lists/*

# Copy the app directory contents into the container
COPY /app /app

# Install any necessary dependencies
RUN pip install --no-cache-dir -r /app/requirements.txt

# Expose the port the app runs on
EXPOSE 5078

# Define the command to run the application
CMD ["python", "app.py"]
