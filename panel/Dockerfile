FROM node:24-slim AS frontend

WORKDIR /panel

COPY package.json package-lock.json ./
RUN npm ci

COPY frontend/ frontend/
RUN npm run build

FROM python:3.12-slim

WORKDIR /panel

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY --from=frontend /panel/static/ static/

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
