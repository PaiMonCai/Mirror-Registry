FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates skopeo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /sync

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync.py .

CMD ["python", "sync.py"]
