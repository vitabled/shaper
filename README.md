# remna-node-quota / shaper

Локальный ограничитель трафика для Remnawave-ноды. Remnawave Panel используется как источник пользователей, а фактический трафик берётся из Xray/rw-core Stats API внутри контейнера `remnanode`.

## Идея

Пример: в Remnawave у всех пользователей общий лимит 500 GiB/day. На конкретной ноде можно поставить отдельный локальный лимит 10 GiB/day. Тогда пользователь сохраняет общий лимит Remnawave, но на этой ноде будет ограничен локальным лимитом.

## Возможности

- несколько inbound на одной ноде;
- разные глобальные лимиты для разных inbound;
- индивидуальный лимит для пользователя на всей ноде;
- индивидуальный лимит для пользователя на конкретном inbound;
- локальный HTTP API;
- смена API-токена через API;
- dry-run режим для проверки без блокировок;
- Remnanode TLS/gRPC helper для доступа к `rw-core` API на `127.0.0.1:61000`.

## Важный нюанс учёта

Xray Stats API в текущей схеме отдаёт счётчик вида `user>>>ID>>>traffic>>>uplink/downlink`, то есть агрегированный счётчик пользователя. Поэтому программа умеет применять разные лимиты и удалять пользователя из конкретных inbound, но фактический счётчик трафика общий для пользователя внутри core. Для полностью независимого учёта байтов по каждому inbound нужны разные идентификаторы пользователя на разных inbound, отдельные core-процессы или отдельный источник per-inbound логов.

## Установка

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/vitabled/shaper/main/install.sh)
```

Установщик спросит:

- имя контейнера Remnanode;
- URL панели Remnawave;
- API token Remnawave;
- список inbound через запятую;
- общий или индивидуальный глобальный лимит для каждого inbound;
- включать ли локальный HTTP API;
- слушать API только локально или во внешнюю сеть.

## API-авторизация

API использует Bearer token:

```bash
TOKEN='TOKEN_FROM_/etc/remna-node-quota/config.json'
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/status
```

Если API открыт во внешнюю сеть, используй HTTPS reverse proxy, firewall или VPN. Без HTTPS Bearer-token передаётся в открытом виде.

## API endpoints

### Статус

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/status
```

### Все пользователи и лимиты

```bash
curl -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8765/api/v1/users?refresh=1'
```

### Глобальные лимиты ноды/inbound

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/node/limits
```

### Изменить глобальный лимит inbound

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"limit_gib": 20}' \
  http://127.0.0.1:8765/api/v1/inbounds/VLESS_TCP_REALITY-SEL-RU-1/limit
```

### Изменить лимит пользователя на всей ноде

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"limit_gib": 5}' \
  http://127.0.0.1:8765/api/v1/users/18/limit
```

### Удалить индивидуальный лимит пользователя на всей ноде

```bash
curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8765/api/v1/users/18/limit
```

### Изменить лимит пользователя на конкретном inbound

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"limit_gib": 2}' \
  http://127.0.0.1:8765/api/v1/users/18/inbounds/VLESS_TCP_REALITY-SEL-RU-1/limit
```

### Удалить индивидуальный лимит пользователя на inbound

```bash
curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8765/api/v1/users/18/inbounds/VLESS_TCP_REALITY-SEL-RU-1/limit
```

### Сменить API token

Указать вручную:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"token":"NEW_LONG_TOKEN_VALUE"}' \
  http://127.0.0.1:8765/api/v1/api-token
```

Сгенерировать новый и вернуть в ответе:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"generate":true,"return_token":true}' \
  http://127.0.0.1:8765/api/v1/api-token
```

### Ручной запуск проверки

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/enforce
```
