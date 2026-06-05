# trueconf-sender/Dockerfile
FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libmagic1 \
        && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/trueconf-zabbix-sender

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY trueconf_sender.py .

RUN mkdir -p queue

CMD ["python", "trueconf_sender.py", "--service"]