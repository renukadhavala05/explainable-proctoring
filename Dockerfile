# Container image for the proctoring server.
# All AI runs in the browser, so the server is tiny and needs no GPU.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The platform (Render/Railway/Fly/etc.) injects $PORT and terminates HTTPS.
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
