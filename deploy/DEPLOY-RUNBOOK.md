# Sprint 6m — Runbook: от заказа VPS до запуска

> ⚠️ **2026-07-14 Sprint 7**: VPS-A `<old-vps-domain>` terminate. Единственная
> VPS = `<your-domain>`. Этот Runbook больше **не актуальный**: в нём
> 2-VPS архитектура, описанная для Sprint 6m.2.
> Актуальный чеклист разворачивания одной VPS: `deploy/PRE-DEPLOY-CHECKLIST.md`.
> История миграции: `notes/technical/sprint-7-architecture-simplification.md`.
>
> Этот файл **оставлен как есть** (MEMORY.md правило: «не дописывать задним
> числом, см. git log для истории»). Использовать ТОЛЬКО для референса на
> 2-VPS-инструкции если ты поднимаешь вторую VPS для эксперимента.

> **Цель**: пошаговое руководство для DD. Каждый блок — законченное действие. После выполнения блока — галочка.

**Расчётное время**: 2–4 часа (без учёта ожидания DNS и SSL).
**Порядок**: строго сверху вниз.

---

## Фаза 0: Что уже готово (локально)

Эти файлы лежат в `<workspace>/.openclaw/workspace/deploy/`:

```
deploy/
├── vps-a-setup.sh          — базовая настройка VPS-A
├── vps-b-setup.sh           — базовая настройка VPS-B
├── openclaw-state-init.sh   — первичный seed openclaw-state репо
├── vps-a-state-sync.sh      — git-pull + git-push на VPS-A
├── sprint-6m-security-audit.md  — чеклист на 130+ пунктов
└── DEPLOY-RUNBOOK.md        — этот файл
```

---

## Фаза 1: Заказ VPS на Selectel

### 1.1. Аккаунт

1. Зайти на [selectel.ru](https://selectel.ru) → создать аккаунт / залогиниться
2. Пополнить баланс (~1500–2000 ₽ на старт: VPS-A 650₽ + VPS-B 400₽ + запас)

### 1.2. VPS-A (<old-vps-domain>)

| Параметр | Значение |
|---|---|
| **Имя** | `<old-vps-host>` |
| **Локация** | Москва (или ближайшая) |
| **Тариф** | **Cloud-2** (2 vCPU, **4 GB RAM**, 50 GB NVMe) = **650 ₽/мес** |
| **ОС** | Ubuntu 22.04 LTS |
| **Сеть** | Белый IP (выделенный), IPv4 |
| **SSH-ключ** | Добавить **свой** публичный ключ ed25519 |

> ⚠️ **Тариф 2 GB — НЕ брать**. Gateway уже жрёт 1.4 GB, нужен запас.

После создания сохранить:
- `IP адрес` VPS-A
- `Логин` (root)

### 1.3. VPS-B (<your-domain>)

| Параметр | Значение |
|---|---|
| **Имя** | `<vps-host>` |
| **Локация** | Та же, что VPS-A |
| **Тариф** | **Cloud-2** (2 vCPU, 4 GB RAM, 50 GB NVMe) = **650 ₽/мес** |
| **ОС** | Ubuntu 22.04 LTS |
| **Сеть** | Белый IP (выделенный), IPv4 |
| **SSH-ключ** | Свой ed25519 ключ |

> DD решение 2026-07-11: оба VPS на **одинаковом тарифе** 4 GB / 650₽.
> Это даёт запас RAM под MariaDB + WP (VPS-B с 2 GB было впритык) и под LLM-heavy cron jobs (VPS-A).

### 1.4. Object Storage (бэкапы)

В **той же панели Selectel** → раздел «Объектное хранилище»:

| Параметр | Значение |
|---|---|
| **Тариф** | Холодное хранилище (Cold) — ~1,7 ₽/ГБ/мес |
| **Бакет** | `mf-backups` |
| **Регион** | Тот же, что VPS |
| **Доступ** | S3-совместимый (через `s3cmd` или `aws-cli`) |
| **Access key + Secret** | Создать в панели, положить в `/opt/<deploy-user>/.env`: `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_ENDPOINT=<region>.selcdn.ru`, `S3_BUCKET=mf-backups` |

**Что бэкапим** (cron 04:00 MSK на VPS-B, скрипт `deploy/vps-b-backup.sh`):
- MariaDB (все БД)
- SQLite `news_memory.db`
- WP uploads (`/var/www/.../wp-content/uploads/`)
- Конфиги (nginx, php-fpm, systemd units, cron)

**Что НЕ бэкапим**: секреты (`.env`, `/etc/openclaw/openclaw.env`) — DD восстанавливает вручную.

**Lifecycle policy** (настраивается в панели Selectel):
- 0–7 дней: Hot (быстрый доступ)
- 7–30 дней: переход в Cold
- 90+ дней: автоматическое удаление

**Ожидаемый объём**: 2-5 ГБ в сжатом виде → **<50 ₽/мес**.

**Restore**: см. `deploy/RESTORE.md`. Quarterly restore drill обязателен.

### 1.5. Домены (если ещё не куплены)

Купить через Selectel (REG.ru как реселлер):

| Домен | Назначение |
|---|---|
| `<old-vps-domain>` | VPS-A |
| `<your-domain>` | VPS-B |

DNS на Selectel (стандартный, не проприетарный):
- `<old-vps-domain>` → A-запись → IP VPS-A
- `<your-domain>` → A-запись → IP VPS-B

**Ожидание DNS propagation**: 5–30 мин.

---

## Фаза 2: Локальная подготовка (пока VPS создаются)

### 2.1. SSH-ключи (если нет)

```bash
# Сгенерить если ~/.ssh/id_ed25519 не существует
ssh-keygen -t ed25519 -C "<your-hostname>" -f ~/.ssh/id_ed25519
```

### 2.2. ~/.ssh/config — записи для удобства

```bash
cat >> ~/.ssh/config <<'EOF'

Host <old-vps-host>
    HostName <old-vps-domain>    # или IP если DNS ещё не propagat
    User root
    Port 22
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 60
    ServerAliveCountMax 3

Host <vps-host>
    HostName <your-domain>
    User root
    Port 22
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 60
    ServerAliveCountMax 3
EOF
```

### 2.3. Инициализировать openclaw-state репо

```bash
# Один раз, на Mac
bash <workspace>/.openclaw/workspace/deploy/openclaw-state-init.sh
```

Что произойдёт:
- Создастся `github.com:<your-org>/openclaw-state` (private)
- Локально появится `~/openclaw-state/` — зеркало workspace
- Запушится initial commit

**После этого**: GitHub → Settings → Deploy keys → Add `~/.ssh/id_ed25519.pub` с правами **Read-only**.

### 2.4. Скопировать setup-скрипты на VPS

```bash
# Когда IP известны
scp ~/.openclaw/workspace/deploy/vps-a-setup.sh root@<VPS-A-IP>:/root/
scp ~/.openclaw/workspace/deploy/vps-b-setup.sh root@<VPS-B-IP>:/root/
```

### 2.5. Проверить .env для VPS-B

Локально:
```bash
cat <workspace>/.openclaw/workspace/media-fabrique-template/.env
```

Нужны секреты:
- `LLM_API_KEY=...`
- `WP_APPLICATION_PASSWORD=...`
- `TELEGRAM_BOT_TOKEN=...`
- `FEEDBACK_SHARED_SECRET=...` (≥32 символа)

**Никогда не коммитить .env в git. Только через scp на VPS-B.**

---

## Фаза 3: VPS-A — базовая настройка

Выполнить от **root** на VPS-A:

```bash
# 1. Установить базовые пакеты, firewall, fail2ban, user openclaw
bash /root/vps-a-setup.sh

# 2. Добавить SSH-ключ openclaw-user в GitHub (deploy key)
#    Скрипт выведет публичный ключ — скопировать в GitHub
#    Settings → openclaw-state → Deploy keys → Add key (Read-only)

# 3. Установить OpenClaw (ВЫБРАТЬ СПОСОБ):
#    A) через npm:
sudo -u openclaw npm install -g openclaw

#    B) через curl:
curl -fsSL https://openclaw.ai/install.sh | bash

#    C) через бинарь:
#    wget https://.../openclaw -O /usr/local/bin/openclaw && chmod +x

# 4. Проверить что openclaw доступен:
openclaw --version

# 5. Настроить openclaw workspace
mkdir -p /var/lib/openclaw/workspace
sudo -u openclaw openclaw configure
#   - Token: ввести после первого запуска (через dashboard)
#   - Model: minimax-portal/your-model-name
#   - Thinking: high

# 6. Настроить git-sync (память между VPS и Mac)
bash /root/vps-a-state-sync.sh

# 7. Перенести workspace на VPS (из ~/openclaw-state)
#    (vps-a-state-sync.sh уже клонировал, проверить)
ls -la /var/lib/openclaw/workspace/

# 8. Включить и запустить gateway
systemctl enable --now openclaw-gateway

# 9. Проверить
ss -tlnp | grep 18789
systemctl status openclaw-gateway

# 10. Проверить SSH-туннель с Mac
ssh -L 18789:localhost:18789 root@<VPS-A-IP>
#    (в браузере): http://localhost:18789
#    Control UI должен открыться
```

### 3.1. TODO: Install OpenClaw (выбрать способ)

В `vps-a-setup.sh` есть placeholder `>>> INSTRUCTION <<<` — **самый важный незакрытый вопрос**. Перед деплоем:
- [ ] Определиться со способом установки OpenClaw на VPS (npm / curl / бинарь)
- [ ] Записать точные команды в секцию TODO скрипта

---

## Фаза 4: VPS-B — базовая настройка

Выполнить от **root** на VPS-B:

```bash
# 1. Базовая настройка: nginx, PHP, MariaDB, firewall, fail2ban
bash /root/vps-b-setup.sh

# 2. Скопировать .env (СЕКРЕТНО — через защищённый канал)
#    НЕ через GitHub, НЕ через email
scp <workspace>/.openclaw/workspace/media-fabrique-template/.env \
    root@<VPS-B-IP>:/opt/<deploy-user>/.env

# 3. Защитить .env
ssh root@<VPS-B-IP> "chown <deploy-user>:<deploy-user> /opt/<deploy-user>/.env && chmod 600 /opt/<deploy-user>/.env"

# 4. Склонировать media-fabrique-template
sudo -u <deploy-user> git clone https://github.com/<your-org>/media-fabrique-template.git \
    /opt/<deploy-user>/media-fabrique-template

# 5. Python venv + зависимости
sudo -u <deploy-user> /opt/<deploy-user>/.venv/bin/pip install -r \
    /opt/<deploy-user>/media-fabrique-template/requirements.txt

# 6. Раскомментировать cron-jobs в /etc/cron.d/<project>-pipeline
#    (убрать # перед каждой строкой)
nano /etc/cron.d/<project>-pipeline

# 7. Настроить nginx site для <your-domain>
#    Пример конфига — ниже
nano /etc/nginx/sites-available/<your-domain>

# 8. SSL
certbot --nginx -d <your-domain> -d www.<your-domain>

# 9. Перенести uploads из LocalWP
rsync -avz --exclude '*.log' \
    /Applications/Local.app/Contents/Resources/Contents/lib/stack/mysql/var/ \
    root@<VPS-B-IP>:/var/www/<your-domain>/wp-content/uploads/
#    ↑ это примерный путь, уточнить на Mac

# 10. wp-cli: создать wp-config.php + установить WP
sudo -u www-data wp config create \
    --dbname=wp_media<deploy-user> \
    --dbuser=wp_<deploy-user> \
    --dbpass="$WP_DB_PASS" \
    --dbhost=localhost \
    --path=/var/www/<your-domain>

sudo -u www-data wp core install \
    --url=https://<your-domain> \
    --title="Media Fabrique" \
    --admin_user=<project> \
    --admin_password="<сгенерировать 20+ символов>" \
    --admin_email=admin@<your-domain> \
    --path=/var/www/<your-domain>

# 11. WP Application Password
#    WP Admin → Users → Profile → Application Passwords → создать
#    Записать в /opt/<deploy-user>/.env: WP_APPLICATION_PASSWORD=<созданный>
```

### 4.1. Nginx конфиг (минимальный)

`/etc/nginx/sites-available/<your-domain>`:

```nginx
server {
    listen 80;
    server_name <your-domain> www.<your-domain>;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name <your-domain> www.<your-domain>;

    root /var/www/<your-domain>;
    index index.php index.html;

    ssl_certificate /etc/letsencrypt/live/<your-domain>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<your-domain>/privkey.pem;

    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    server_tokens off;

    # WP rules
    location / {
        try_files $uri $uri/ /index.php?$args;
    }

    location ~ \.php$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php8.3-fpm.sock;
        include fastcgi_params;
    }

    # xmlrpc.php — закрыть
    location = /xmlrpc.php {
        return 403;
    }

    # uploads
    location /wp-content/uploads/ {
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    access_log /var/log/nginx/media-<deploy-user>.access.log;
    error_log /var/log/nginx/media-<deploy-user>.error.log;
}
```

### 4.2. Feedback receiver — ТЕКУЩИЙ подход (long-poll cron, Sprint 6.5.1)

**Важно**: runbook ранее описывал FastAPI-вебхук для feedback, но текущая реализация
(`feedback_receiver.py`) — это **long-poll cron-скрипт** (Telegram Bot API polling).
Он запускается через `*/1 * * * *` в cron, **не** через systemd/FastAPI.

FastAPI-вебхук (REST `POST /feedback` с `X-Auth`) будет в **отдельном будущем спринте**
после стабилизации long-poll подхода.

Текущий feedback receiver (cron-driven):

```bash
# Проверка: crontab -l -u <deploy-user>
*/1 * * * *  <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python -m media-fabrique-template.feedback_receiver
```

Логи:
```bash
tail -f /var/log/<project>/feedback_receiver.log
```

---

## Фаза 5: Security Audit

**Перед тем как открывать VPS в мир** — прогнать чеклист:

```bash
# На каждом VPS
nano <workspace>/.openclaw/workspace/deploy/sprint-6m-security-audit.md
#   Прогнать секции A, B, C (VPS-A) и A, B, D, E (VPS-B)
#   Блокеры: A, B, C, D, E — без них НЕ открывать 80/443
```

Ключевые проверки:
- [ ] `PasswordAuthentication no` в sshd_config
- [ ] fail2ban активен
- [ ] ufw: только 22 (VPS-A), 22+80+443 (VPS-B)
- [ ] OpenClaw Gateway на 127.0.0.1:18789
- [ ] .env chmod 600, владелец <deploy-user>:www-data (для WP)
- [ ] xmlrpc.php → 403

---

## Фаза 6: Итоговая проверка

### На VPS-A:

```bash
# Gateway живёт
systemctl status openclaw-gateway
ss -tlnp | grep 18789

# SSH-туннель работает с Mac
ssh -L 18789:localhost:18789 root@<old-vps-domain>
# (открыть http://localhost:18789)

# Memory-sync
sudo -u openclaw crontab -l   # должны быть cron-строки
tail -f /var/log/openclaw-state-sync.log

# Telegram бот
curl -s http://localhost:18789/api/status | jq .
```

### На VPS-B:

```bash
# WP доступен
curl -s -o /dev/null -w "%{http_code}" https://<your-domain>

# Cron-jobs
sudo -u <deploy-user> crontab -l

# Morning report test
sudo -u <deploy-user> /opt/<deploy-user>/.venv/bin/python \
    -m media-fabrique-template.main --tick=morning_report

# Feedback API (FastAPI webhook — будущий спринт, пока не реализован)
# Пока работает long-poll cron: /up /down /ideas в Telegram-группе
```

---

## Фаза 7: Что делать если что-то сломалось

### VPS-A не отвечает по SSH
→ Консоль хостера (Selectel) → VNC/Rescue → проверить sshd, firewall, сеть

### Gateway не стартует
```bash
journalctl -u openclaw-gateway -n 100 --no-pager
# Искать: permissions, missing env vars, port already in use
```

### WP ошибка 502
→ nginx/php-fpm не общаются. Проверить сокет:
```bash
ls -la /run/php/php*-fpm.sock
systemctl status php*-fpm
```

### Cron не работает
```bash
sudo -u <deploy-user> /opt/<deploy-user>/.venv/bin/python \
    -m media-fabrique-template.main --tick=fetch
# Смотрим stdout/stderr — что падает
```

### Feedback receiver не пишет в БД
```bash
# Feedback receiver — long-poll cron, работает каждые 60 сек
# Проверить что cron активен
sudo -u <deploy-user> crontab -l
# Лог последних запусков
sudo -u <deploy-user> tail -20 /var/log/<project>/feedback_receiver.log
```

---

## Чеклист «всё запущено»

- [ ] VPS-A: Gateway на 127.0.0.1:18789
- [ ] VPS-A: SSH-туннель с Mac → http://localhost:18789 открывается
- [ ] VPS-A: Telegram бот отвечает в группе
- [ ] VPS-A: memory-sync каждые 5 мин
- [ ] VPS-B: https://<your-domain> открывается
- [ ] VPS-B: WP REST API работает (curl к `/wp/v2/posts`)
- [ ] VPS-B: Cron-jobs (6 шт.) активны
- [ ] VPS-B: Feedback receiver cron активен (`*/1 * * * *` в crontab <deploy-user>)
- [ ] VPS-B: SSL сертификат валидный
- [ ] Все секреты в `/etc/openclaw/openclaw.env` и `/opt/<deploy-user>/.env`

---

## Контакты для emergencies

| Что | Как чинить |
|---|---|
| VPS-A упал | Selectel console → VNC → journalctl |
| WP сломался | nginx error log + WP_DEBUG_LOG |
| Cron завис | `logs/.last_tick` — какие джобы отстали |
| SSL истёк | `certbot renew --dry-run` + `certbot renew` |
| Сеть лагает | `mtr <your-domain>`, `ping 8.8.8.8` |

---

## Связанные документы

- `notes/technical/sprint-6m-security-audit.md` — полный security чеклист
- `notes/openclaw/remote-access.md` — SSH-туннель, как подключиться
- `notes/technical/sprint-6m-monetization.md` — архитектура, зачем всё это
- `MEMORY.md` — актуальный статус проекта