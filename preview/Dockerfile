FROM python:3.12-slim

# LibreOffice for Office → PDF conversion.
# Fonts: liberation (Times/Arial substitutes), dejavu (broad coverage),
# noto-cjk (Asian scripts) — without these, conversions render in fallback faces.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    fonts-liberation \
    fonts-dejavu \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PORT=5000
EXPOSE 5000

# Default command is the Flask server (configured in Phase 3 / Task 17).
# Phase 1 overrides this with `docker run ... python -m pytest` for testing.
CMD ["python", "server.py"]
