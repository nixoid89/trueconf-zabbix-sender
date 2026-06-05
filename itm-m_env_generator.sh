#!/bin/bash

IPV4_REGEXP="(([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.){3}([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])"

error_exit() {
    echo "Произошла ошибка: $1"
    exit 1
}

remove_env() {
    rm .env
}

read_until() {
    error_message=${3:-"Введено некорректное значение! Повторите попытку\n"}
    read -e -p "$1"

    if [[ "$REPLY" =~ $2 ]]; then
      return 0
    fi

    echo -e "$error_message" >&2
    read_until "$1" "$2" "$error_message"
}

if [ -f ".env" ]
    then error_exit "Файл .env уже существует."
fi

echo "Чтобы оставить значение по умолчанию из квадратных скобок, нажмите enter" ; echo

set_db_env_variables() {
    read -p "Укажите адрес СУБД [host.docker.internal]: "
    if [[ -z $REPLY ]]
    then
        echo "DB_SERVER_HOST=host.docker.internal" >> .env ; echo
    else
        echo "DB_SERVER_HOST=$REPLY" >> .env ; echo
    fi

    read -p "Укажите порт CУБД [10265]: "
    if [[ -z $REPLY ]]
    then
        echo "DB_SERVER_PORT=10265" >> .env ; echo
    else
        echo "DB_SERVER_PORT=$REPLY" >> .env ; echo
    fi

    read -p "Укажите имя пользователя CУБД [itmm_user]: "
    if [[ -z $REPLY ]]
    then
        echo "POSTGRES_USER=itmm_user" >> .env ; echo
    else
        echo "POSTGRES_USER=$REPLY" >> .env ; echo
    fi

    read -p "Укажите пароль пользователя CУБД [P@ssw0rd]: "
    if [[ -z $REPLY ]]
    then
        echo "POSTGRES_PASSWORD=P@ssw0rd" >> .env ; echo
    else
        echo "POSTGRES_PASSWORD=$REPLY" >> .env ; echo
    fi

    read -p "Укажите название базы данных [itmm]: "
    if [[ -z $REPLY ]]
    then
        echo "POSTGRES_DB=itmm" >> .env ; echo
    else
        echo "POSTGRES_DB=$REPLY" >> .env ; echo
    fi
}

set_datapk_env_variables() {
    read_until "Укажите адрес DATAPK [127.0.0.1]: " "^($IPV4_REGEXP)?$"
    if [[ -z $REPLY ]]; then
        echo "DATAPK_HOST=https://127.0.0.1" >> .env ; echo
    else
        echo "DATAPK_HOST=https://$REPLY" >> .env ; echo
    fi

    read -p "Укажите порт DATAPK [443]: "
    if [[ -z $REPLY ]]; then
        echo "DATAPK_PORT=443" >> .env ; echo
    else
        echo "DATAPK_PORT=$REPLY" >> .env ; echo
    fi

    read_until "Укажите мажорную версию DATAPK [19]: " "^(16|17|18|19|19_fstec)?$" "Некорректная версия! Допустимые значения: 16, 17, 18, 19, 19_fstec\n"
    if [[ -z $REPLY ]]; then
        echo "DATAPK_VERSION=19" >> .env ; echo
    else
        echo "DATAPK_VERSION=$REPLY" >> .env ; echo
    fi

    read -p "Укажите пользователя DATAPK [datapk]: "
    if [[ -z $REPLY ]]; then
        echo "DATAPK_USER=datapk" >> .env ; echo
    else
        echo "DATAPK_USER=$REPLY" >> .env ; echo
    fi

    read -p "Укажите пароль пользователя DATAPK [datapk]: "
    if [[ -z $REPLY ]]; then
        echo "DATAPK_PASSWORD=datapk" >> .env ; echo
    else
        echo "DATAPK_PASSWORD=$REPLY" >> .env ; echo
    fi
}

set_srv_env_variables() {
    read -p "Укажите период запроса данных истории, доступности узлов сети, обнаружения и авторегистрации с сервера агентов ITM-RM сервером мониторинга ITM-M в секундах [60]: "
    if [[ -z $REPLY ]]
    then
        ITMM_PROXYDATAFREQUENCY="60" ; echo
    else
        ITMM_PROXYDATAFREQUENCY=$REPLY ; echo
    fi

    cat >> .env <<EOM
# ITMM_STARTREPORTWRITERS=0
# ITMM_DEBUGLEVEL=3
ITMM_STARTPOLLERS=15
# ITMM_STARTIPMIPOLLERS=3
ITMM_STARTPREPROCESSORS=3
ITMM_STARTPOLLERSUNREACHABLE=10
# ITMM_STARTTRAPPERS=5
ITMM_STARTPINGERS=5
ITMM_ENABLE_SNMP_TRAPS=true
# ITMM_HOUSEKEEPINGFREQUENCY=1
# ITMM_MAXHOUSEKEEPERDELETE=5000
# ITMM_PROBLEMHOUSEKEEPINGFREQUENCY=60
# ITMM_SENDERFREQUENCY=30
# ITMM_CACHESIZE=256M
# ITMM_CACHEUPDATEFREQUENCY=60
# ITMM_STARTDBSYNCERS=4
# ITMM_HISTORYCACHESIZE=128M
# ITMM_HISTORYINDEXCACHESIZE=64M
# ITMM_HISTORYSTORAGEDATEINDEX=0
# ITMM_TRENDCACHESIZE=128M
# ITMM_TRENDFUNCTIONCACHESIZE=4M
# ITMM_VALUECACHESIZE=128M
ITMM_TIMEOUT=20
ITMM_STARTPROXYPOLLERS=5
ITMM_PROXYCONFIGFREQUENCY=900
ITMM_PROXYDATAFREQUENCY=$ITMM_PROXYDATAFREQUENCY
EOM
}

set_web_env_variables() {
    PHP_TZ=`timedatectl | grep -o -P '(?<=Time zone: ).*(?= \()'`

    cat >> .env <<EOM
ITMM_MEMORYLIMIT=256M
ITMM_POSTMAXSIZE=32M
ITMM_UPLOADMAXFILESIZE=32M
# Timezone one of: http://php.net/manual/en/timezones.php
PHP_TZ=$PHP_TZ
EOM
}

generate_env_file() {
    if [ "$1" = "dev" ]
    then
        echo "#----------Для разработки----------" >> .env
        echo "COMPOSE_FILE=docker-compose.yaml:docker-compose.dev.yaml" >> .env
        echo "ITMM_IMAGES_TAG=master" >> .env
    else
        echo "COMPOSE_FILE=docker-compose.release.yaml" >> .env
    fi

    read -p "Настроить переменные для веб-интерфейса? (Y/n)" -n 1 -s -r yn; echo
    echo -e "\n#----------Web UI----------" >> .env
    if [[ "$yn" =~ ^([yY])$ ]] || [[ -z "$yn" ]]
    then
        read -e -p "Укажите порт для подключения к веб-интерфейсу по HTTP [80]: "
        if [[ -z "$REPLY" ]]
        then
            echo "ITMM_FRONT_HTTP_PORT=80" >> .env
        else
            echo "ITMM_FRONT_HTTP_PORT=$REPLY" >> .env
        fi

        read -e -p "Укажите порт для подключения к веб-интерфейсу по HTTPS [443]: "
        if [[ -z "$REPLY" ]]
        then
            echo "ITMM_FRONT_HTTPS_PORT=443" >> .env ; echo
        else
            echo "ITMM_FRONT_HTTPS_PORT=$REPLY" >> .env ; echo
        fi
    else
        echo "ITMM_FRONT_HTTP_PORT=80" >> .env
        echo "ITMM_FRONT_HTTPS_PORT=443" >> .env ; echo
    fi

    read -p "Нужна синхронизация с DATAPK? (Y/n)" -n 1 -s -r yn; echo
    if [[ "$yn" =~ ^([yY])$ ]] || [[ $yn = "" ]]
    then
        echo -e "\n#----------Синхронизация----------" >> .env
        echo "SYNCHRONIZATION_ENABLED=true" >> .env ; echo
        read -p "Укажите период между автоматическими синхронизациями в секундах [3600]: "
        if [[ -z $REPLY ]]
        then
            echo "SYNCHRONIZATION_DELAY_SECONDS=3600" >> .env ; echo
        else
            echo "SYNCHRONIZATION_DELAY_SECONDS=$REPLY" >> .env ; echo
        fi

        echo -e "\n#----------Настройки DATAPK----------" >> .env
        set_datapk_env_variables
    else
        echo -e "\n#----------Синхронизация----------" >> .env
        echo "SYNCHRONIZATION_ENABLED=false" >> .env
        echo "SYNCHRONIZATION_DELAY_SECONDS=3600" >> .env

        echo -e "\n#----------Настройки DATAPK----------" >> .env
        echo "DATAPK_HOST=https://127.0.0.1" >> .env
        echo "DATAPK_PORT=443" >> .env
        echo "DATAPK_VERSION=19" >> .env
        echo "DATAPK_USER=datapk" >> .env
        echo "DATAPK_PASSWORD=datapk" >> .env
        echo
    fi

    echo -e "\n#----------Настройки ITMM----------" >> .env
    echo "ITMM_USER=itm" >> .env
    echo "ITMM_PASSWORD=ChangeMe" >> .env

    echo -e "\n#----------Сети DOCKER----------" >> .env
    read_until "Укажите адрес подсети контейнеров [172.16.239.0/24]: " "^($IPV4_REGEXP/([0-9]|[12][0-9]|3[0-2]))?$"
    if [[ -z $REPLY ]]
    then
        echo "ITMM_SUBNET=172.16.239.0/24" >> .env ; echo
    else
        echo "ITMM_SUBNET=$REPLY" >> .env ; echo
    fi

    read_until "Укажите шлюз подсети контейнеров [172.16.239.1]: " "^($IPV4_REGEXP)?$"
    if [[ -z $REPLY ]]
    then
      echo "ITMM_SUBNET_GATEWAY=172.16.239.1" >> .env
    else
      echo "ITMM_SUBNET_GATEWAY=$REPLY" >> .env
    fi

    echo -e "\n#----------Настройки подключения к БД----------" >> .env
    set_db_env_variables

    echo -e "\n#----------Настройки сервера ITM-M----------" >> .env
    set_srv_env_variables

    echo -e "\n#----------Настройки веб-интерфейса ITM-M----------" >> .env
    set_web_env_variables
}

generate_env_file $1