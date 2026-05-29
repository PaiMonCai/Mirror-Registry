FROM node:24-slim AS frontend

WORKDIR /panel

COPY panel/package.json panel/package-lock.json ./
RUN npm ci

COPY panel/frontend/ frontend/
RUN npm run build

FROM python:3.12-slim

WORKDIR /app

COPY panel/requirements.txt panel/requirements.txt
RUN pip install --no-cache-dir -r panel/requirements.txt

COPY mirror_registry_core/ mirror_registry_core/
COPY panel/*.py panel/
COPY --from=frontend /panel/static/ panel/static/

CMD ["uvicorn", "panel.main:app", "--host", "0.0.0.0", "--port", "8080"]
