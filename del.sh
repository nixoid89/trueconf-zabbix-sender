#!/bin/bash

# Остановка контейнера
docker compose -f docker-compose.yml stop trueconf-sender

# Получение ID контейнера
container_id=$(docker ps -aqf "name=trueconf-sender")

# Удаление контейнера
if [ -n "$container_id" ]; then
    docker rm "$container_id"
else
    echo "Контейнер не найден."
fi

# Удаление образа
docker rmi trueconf-sender:local

docker build -t trueconf-sender:local ./trueconf-sender

docker compose -f docker-compose.yml up -d  trueconf-sender

docker logs trueconf-sender
