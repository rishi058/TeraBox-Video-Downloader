FROM node:22-slim AS mini-app-build

WORKDIR /mini-app

COPY mini-app/package.json mini-app/package-lock.json ./
RUN npm ci

COPY mini-app/ ./
RUN npm run build


FROM python:3.12-slim

WORKDIR /app

RUN echo "==== MY DOCKERFILE ===="

# Install system dependencies (from apt.txt)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY --from=mini-app-build /mini-app/dist ./mini-app/dist

CMD ["python", "main.py"]