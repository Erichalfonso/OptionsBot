FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY config.py .
COPY logger_setup.py .
COPY parser.py .
COPY broker.py .
COPY notifier.py .
COPY positions.py .
COPY risk_manager.py .
COPY bot.py .

CMD ["python", "-u", "bot.py"]
