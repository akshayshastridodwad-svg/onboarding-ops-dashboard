# Use the official lightweight Python image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file first to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the default Streamlit port
EXPOSE 8501

# Define environment variables (Defaults - can be overridden at runtime)
ENV N8N_WEBHOOK_URL="https://n8n-conc.razorpay.com/webhook"
ENV N8N_WEBHOOK_PATH="mid_intelligence"

# Command to run the application
CMD ["streamlit", "run", "appversion2.py", "--server.port=8501", "--server.address=0.0.0.0"]
