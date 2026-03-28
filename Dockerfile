FROM node:20-alpine AS web_builder
WORKDIR /web
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build:web

FROM python:3.12-slim
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DATA_ROOT=/data

COPY engine/requirements.txt /app/engine/requirements.txt
RUN pip install --no-cache-dir -r /app/engine/requirements.txt

COPY engine /app/engine
COPY --from=web_builder /web/dist /app/dist

EXPOSE 8766

# Railway usually sets PORT; fallback to 8766 for local parity.
CMD ["sh", "-c", "cd /app/engine && python3 -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8766}"]

