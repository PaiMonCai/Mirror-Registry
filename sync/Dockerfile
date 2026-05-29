FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates skopeo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY sync/requirements.txt sync/requirements.txt
RUN pip install --no-cache-dir -r sync/requirements.txt

COPY mirror_registry_core/ mirror_registry_core/
COPY sync/*.py sync/

CMD ["python", "-m", "sync.sync"]
