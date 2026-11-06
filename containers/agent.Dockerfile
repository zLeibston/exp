# Fixed controller / Meta runtime; no task-domain source or test packages needed.
FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git bash ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /engine
