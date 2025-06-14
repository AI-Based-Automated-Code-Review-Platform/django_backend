# Dockerfile for Django Application

# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory in the container
WORKDIR /app

# Install system dependencies (if any)
# RUN apt-get update && apt-get install -y --no-install-recommends \
#    postgresql-client \
#    && rm -rf /var/lib/apt/lists/*


# Copy the requirements file into the container at /app
COPY requirements.txt /app/

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the current directory contents into the container at /app
COPY . /app/

# Expose the port the app runs on
EXPOSE 8000

# Command to run the application (will be overridden by docker-compose for development)
# For production, you might use Gunicorn or uWSGI
# CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
