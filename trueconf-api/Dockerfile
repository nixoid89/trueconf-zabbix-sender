# trueconf-api/Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Копируем и устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем скрипт
COPY api_server.py .

# Создаем директорию для очереди
RUN mkdir -p /app/queue

EXPOSE 8081

CMD ["python", "api_server.py"]