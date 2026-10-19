# ЛР3. Моніторинг мережевої інфраструктури

Стенд піднімається поруч із проєктом ЛР1 і не змінює його файли. Перед запуском проєкт ЛР1 має бути створений, бо мережа `frontend` зовнішня.

## Запуск

Скопіюйте `.env.example` в `.env` і задайте паролі. Community SNMP для цього стенда обмежена мережею `172.31.0.0/24`.

```text
docker compose up -d --build
```

Сервіс `bootstrap` створює вузли, шаблони, веб-сценарій, тригери, користувача Grafana і дашборд. Пароль адміністратора Zabbix лишається заводським `zabbix`; bootstrap його не читає з `.env` і не змінює. Повторний запуск входить через API-токен у `bootstrap/state`. Хід роботи видно так:

```text
docker compose logs -f bootstrap
```

На хост опубліковано веб-інтерфейс Zabbix `8081` і Grafana `3000`. База Zabbix, сервер, агент і SNMP назовні не відкриті.

## Склад

```text
docker-compose.yml                         сервіси моніторингу
snmp/Dockerfile                            образ net-snmp
snmp/entrypoint.sh                         snmpd.conf із обмеженням мережі
bootstrap/bootstrap.py                     налаштування через API Zabbix і Grafana
grafana/provisioning/plugins/zabbix.yaml   увімкнення плагіна
templates/                                 експорт шаблонів Zabbix
grafana/dashboards/lab3.json               експорт дашборда
```
