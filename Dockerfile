# syntax=docker/dockerfile:1
FROM node:22-slim AS web
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ARG TARGETARCH
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl unzip ca-certificates \
 && case "${TARGETARCH}" in \
      amd64) AWS_ZIP=awscli-exe-linux-x86_64.zip ;; \
      arm64) AWS_ZIP=awscli-exe-linux-aarch64.zip ;; \
      *) echo "unsupported arch ${TARGETARCH}" && exit 1 ;; \
    esac \
 && curl -fsSL "https://awscli.amazonaws.com/${AWS_ZIP}" -o /tmp/awscliv2.zip \
 && unzip -q /tmp/awscliv2.zip -d /tmp && /tmp/aws/install \
 && rm -rf /tmp/aws /tmp/awscliv2.zip /var/lib/apt/lists/* \
 && aws --version 2>&1 | grep -q "aws-cli/2" \
 && git config --system --add safe.directory '*'
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Local image only. requirements-browser.txt is playwright, which downloads
# Chromium. Do not install requirements-local.txt here: mcp needs a newer
# pydantic than requirements.txt pins, and Railway never builds this image.
COPY backend/requirements-browser.txt ./
RUN pip install --no-cache-dir -r requirements-browser.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*
ENV SURGICALAI_IN_DOCKER=1
COPY backend/ ./
COPY --from=web /app/frontend/dist /app/frontend/dist
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
