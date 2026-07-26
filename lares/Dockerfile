# --- frontend build ---
FROM node:22-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- python deps, compiled once, isolated from the runtime image ---
FROM python:3.12-slim AS python-deps
RUN apt-get update && apt-get install -y --no-install-recommends gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r /tmp/requirements.txt

# --- runtime ---
FROM python:3.12-slim
# iputils-ping: needed for ping-type uptime targets. Docker's default
# capability set already includes NET_RAW, so no extra container
# privileges are required for this to work.
RUN apt-get update && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*
COPY --from=python-deps /install /usr/local
WORKDIR /app
COPY backend ./backend
COPY --from=frontend-build /app/frontend/dist ./frontend/dist
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["/app/docker-entrypoint.sh"]
