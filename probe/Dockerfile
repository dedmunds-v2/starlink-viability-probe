FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends traceroute \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY probe.py path.py targets.json ./
EXPOSE 9108
CMD ["python", "probe.py", "--config", "targets.json", "--port", "9108", \
     "--interval", "10", "--path-interval", "300"]
