FROM python:3.12-slim

WORKDIR /app

# System deps for pandas/openpyxl/psycopg2 wheels build cleanly on slim.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

WORKDIR /app/backend

# Render sets $PORT at runtime; default 8000 covers running the image
# anywhere else (docker run -p 8000:8000, another host, etc).
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
