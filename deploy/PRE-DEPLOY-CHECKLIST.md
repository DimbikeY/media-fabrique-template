# Pre-Deploy Checklist — Sprint 7 (VPS-B единственная VPS)

> **Sprint 7 (2026-07-14)**: VPS-A удалена из архитектуры. Этот чеклист
> теперь — только для VPS-B (`<your-domain>`).

> Если ты разворачиваешь **с нуля новый инстанс** на новой VPS — этот
> файл используй как есть (одна VPS, никаких SSH-туннелей между нодами).

## Предусловия (твоя сторона)

- [ ] DNS propagatировался: `dig <your-domain> +short` → IP VPS-B
- [ ] DNS не обязан содержать `<old-vps-domain>` (VPS-A удалена)
- [ ] Object Storage bucket `mf-backups` создан, S3 keys получены
- [ ] TG bot token (от @BotFather) скопирован в безопасное место
- [ ] SSH-ключ `~/.ssh/id_ed25519` готов (или создать: `ssh-keygen -t ed25519`)
- [ ] `~/.ssh/config` настроен (см. ниже)

---

## Шаг 0 — `~/.ssh/config` (на Mac)

Добавить:

```
Host <vps-host>
    HostName <vps-host>   # или IP <VPS-B-IP> если DNS ещё не propagatировался
    User root
    Port 22
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 60
    ServerAliveCountMax 3

# Туннель для OpenClaw Control UI (порт 18790, потому что 18789 занят
# локальным OpenClaw webchat-агентом на Mac).
Host <vps-host>-tunnel
    HostName <vps-host>
    User root
    Port 22
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesInterval 60
    ServerAliveCountMax 3
    LocalForward 18790 127.0.0.1:18789
```

**НЕ добавляй** блоки `<old-vps-host>`/`<old-vps-tunnel>` — VPS-A не существует.
Если ты разворачиваешь с нуля на новой VPS, используй ровно один alias.

---

## Шаг 1 — Создать cloud-VPS (Selectel Cloud-2)

- 2 vCPU / 4 GB / 50 GB NVMe → 650 ₽/мес
- Ubuntu 24.04 LTS (noble)
- Public IPv4
- Записать root password (Selectel отправит на email)
- SSH ключи: добавить свой `~/.ssh/id_ed25519.pub` через панель Selectel

После создания:
```bash
ssh root@<NEW_VPS_IP> 'hostname; uptime; cat /etc/os-release | head -3'
```

---

## Шаг 2 — Базовый setup VPS

### 2.1 Обновить пакеты и установить базу

```bash
ssh root@<NEW_VPS_IP>
apt-get update && apt-get upgrade -y
apt-get install -y ca-certificates curl gnupg nginx mariadb-server \
    php-fpm php-mysql php-curl php-gd php-mbstring php-xml php-zip \
    python3 python3-venv python3-dev build-essential \
    ufw fail2ban unattended-upgrades certbot python3-certbot-nginx
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
```

### 2.2 Node.js 22 (для OpenClaw Gateway)

```bash
mkdir -p /etc/apt/keyrings
curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
  | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg
echo "Types: deb
URIs: https://deb.nodesource.com/node_22.x
Suites: nodistro
Components: main
Architectures: amd64
Signed-By: /usr/share/keyrings/nodesource.gpg" > /etc/apt/sources.list.d/nodesource.sources
apt-get update && apt-get install -y nodejs
node --version  # → v22.23.1+
```

### 2.3 OpenClaw (глобально)

```bash
npm install -g openclaw@2026.6.11
openclaw --version  # → OpenClaw 2026.6.11 (e085fa1)
```

### 2.4 Создать пользователей `openclaw` (uid 999) и `<deploy-user>`

```bash
groupadd -g 989 openclaw  # если ещё нет
useradd -u 999 -g 989 -m -d /var/lib/openclaw -s /bin/bash openclaw
useradd -m -d /home/<deploy-user> -s /bin/bash <deploy-user>  # если ещё нет
usermod -aG <deploy-user> openclaw   # openclaw может работать с /opt/<deploy-user>
chmod 700 /var/lib/openclaw /home/<deploy-user>
```

### 2.5 Каталоги `<deploy-user>` (для проекта)

```bash
mkdir -p /opt/<deploy-user>/{media-fabrique-template,scripts,data/images,data/daily-stats,.venv,bin,ops}
chmod -R 775 /opt/<deploy-user>  # openclaw + <deploy-user> могут писать
chmod 700 /opt/<deploy-user>/.venv /opt/<deploy-user>/data
mkdir -p /var/log/<project> /var/backups/<deploy-user>
chmod 755 /var/log/<project> /var/backups/<deploy-user>
```

### 2.6 Клонировать проект

DD делает через ssh-tunnel или физически:
```bash
sudo -u <deploy-user> bash -lc '
  cd /opt/<deploy-user>/media-fabrique-template
  git clone git@github.com:<your-org>/media-fabrique-template.git .
  python3 -m venv /opt/<deploy-user>/.venv --system-site-packages
  /opt/<deploy-user>/.venv/bin/pip install -r /opt/<deploy-user>/media-fabrique-template/requirements.txt
'
```

### 2.7 `/opt/<deploy-user>/.env`

DD копирует `.env` через scp или вручную через nano. **Никогда не коммитить в git**.

```bash
chmod 600 /opt/<deploy-user>/.env
chown <deploy-user>:<deploy-user> /opt/<deploy-user>/.env
```

### 2.8 MariaDB + WordPress

```bash
mysql_secure_installation
mysql -u root <<'SQL'
CREATE DATABASE wordpress CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'wp'@'localhost' IDENTIFIED BY '<WP_DB_PASSWORD>';
GRANT ALL PRIVILEGES ON wordpress.* TO 'wp'@'localhost';
FLUSH PRIVILEGES;
SQL
```

Установить WP через `wp core download` или вручную. Настройка — как в Sprint 6m docs.

### 2.9 Объектное хранилище + backup

DD настраивает S3 keys, `/etc/<deploy-user>-backup/credentials`, `/etc/cron.d/<deploy-user>-backup`.

### 2.10 SSL (Let's Encrypt)

```bash
certbot --nginx -d <your-domain> -d www.<your-domain> --redirect --hsts --staple-ocsp
```

---

## Шаг 3 — Migrate OpenClaw state (если переезжаешь с старого VPS)

Если старый VPS-B ещё жив, скопируй state:

```bash
# На старом VPS-B
ssh old<vps-host> 'cd /var/lib/openclaw
tar --exclude="workspace.bak-*" --exclude=".openclaw/identity" \
    -czf /tmp/openclaw-state.tar.gz .openclaw .ssh .cache .gitconfig'

# На новом VPS-B
scp old<vps-host>:/tmp/openclaw-state.tar.gz /tmp/
scp /tmp/openclaw-state.tar.gz root@<NEW_IP>:/tmp/
ssh root@<NEW_IP> 'cd /var/lib/openclaw
tar -xzf /tmp/openclaw-state.tar.gz
chown -R openclaw:openclaw .openclaw .ssh .cache .gitconfig'
```

Миграция **webhookUrl**: после `systemctl start openclaw-gateway` сделай
patch в `/var/lib/openclaw/.openclaw/openclaw.json`:
```python
json['channels']['telegram']['webhookUrl'] = 'https://<NEW_DOMAIN>/telegram-webhook'
```

---

## Шаг 4 — Systemd: openclaw-gateway.service

```bash
# /etc/systemd/system/openclaw-gateway.service
cat > /etc/systemd/system/openclaw-gateway.service <<'UNITEOF'
[Unit]
Description=OpenClaw Gateway
After=network-online.target
Wants=network-online.target

[Service]
ExecStartPre=/opt/<deploy-user>-ops/bin/mf-pre-start.sh
Type=simple
User=openclaw
Group=openclaw
WorkingDirectory=/opt/<deploy-user>/media-fabrique-template/workspace
ExecStart=/usr/bin/openclaw gateway --port 18789 --bind loopback
TimeoutStartSec=60
Restart=always
RestartSec=5
EnvironmentFile=-/etc/openclaw/openclaw.env

NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/openclaw/.cache /var/lib/openclaw/.openclaw /opt/<deploy-user>/media-fabrique-template /opt/<deploy-user>-ops /var/log/<project>

[Install]
WantedBy=multi-user.target
UNITEOF

mkdir -p /etc/systemd/system/openclaw-gateway.service.d
cat > /etc/systemd/system/openclaw-gateway.service.d/override.conf <<'OVEREOF'
[Service]
ProtectSystem=strict
ReadWritePaths=/var/lib/openclaw
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=true
MemoryMax=2G
PrivateTmp=true
OVEREOF
systemctl daemon-reload
systemctl enable openclaw-gateway
systemctl start openclaw-gateway
```

---

## Шаг 5 — nginx location для OpenClaw webhook

В `/etc/nginx/sites-enabled/<your-domain>` (или эквивалент), **внутри HTTPS-секции** добавить **перед** общим `location / {`:

```
location = /telegram-webhook {
    proxy_pass http://127.0.0.1:8787;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
    proxy_send_timeout 60s;
    proxy_buffering off;
}
```

Затем:
```bash
nginx -t && nginx -s reload
```

---

## Шаг 6 — Системный cron

```bash
cat > /etc/cron.d/<project>-pipeline <<'CRONEOF'
# /etc/cron.d/<project>-pipeline
SHELL=/bin/bash
PATH=/opt/<deploy-user>/.venv/bin:/usr/local/bin:/usr/bin:/bin
PYTHONUNBUFFERED=1

*/30 * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=fetch    >> /var/log/<project>/fetch.log    2>&1
*/7  * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=rewrite  >> /var/log/<project>/rewrite.log  2>&1
*/5  * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=publish  >> /var/log/<project>/publisher.log 2>&1
0    * * * *   <deploy-user>  cd /opt/<deploy-user>/media-fabrique-template && .venv/bin/python main.py tick=janitor  >> /var/log/<project>/janitor.log  2>&1
CRONEOF

# Optional: morning report теперь тоже В ЛОКАЛЬНОЙ VPS
cat > /etc/cron.d/mf-morning-report <<'MORNINGEOF'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
MAILTO=""
TZ=Europe/Moscow

0 7 * * *   <deploy-user>  /opt/<deploy-user>-ops/bin/mf-morning-report.sh >> /var/log/<project>/mf-morning.log 2>&1
MORNINGEOF
```

---

## Шаг 7 — smoke-проверка

```bash
# Локально на VPS
ssh root@<your-domain> 'systemctl status openclaw-gateway'
# → Active: active

ssh root@<your-domain> 'curl -sf http://127.0.0.1:18789/health || echo FAIL'
# → OK response

ssh root@<your-domain> '.venv/bin/python -m morning_report --json --since 24h | python3 -m json.tool | head -20'
# → JSON with .published.by_category

ssh root@<your-domain> 'curl -sS https://api.telegram.org/bot<TOKEN>/getWebhookInfo | python3 -m json.tool'
# → "url": "https://<your-domain>/telegram-webhook"
# → "ip_address": <VPS_PUBLIC_IP>
```

С Mac:

```bash
ssh -N -L 18801:127.0.0.1:18789 <vps-host> &
sleep 2
curl -sf http://127.0.0.1:18801/health
# → OK
```

---

## Что вышло из употребления (Sprint 7)

- ❌ отдельная VPS-A (<old-vps-domain>) — terminate через Selectel panel
- ❌ SSH-туннель между VPS-A → VPS-B для morning-report
- ❌ ~/.ssh/mf/agent-mf ключ — удалён с Mac
- ❌ openclaw user на VPS-A — userdel
- ❌ /opt/<deploy-user>-ops на VPS-A — rm
- ❌ mf-morning-report cron на VPS-A — .disabled-by-sprint7-*

## Что осталось как было

- ✅ `<deploy-user>-ssh-gate.sh` wrapper на <deploy-user>@<vps-host> — на случай отката
- ✅ OpenClaw cron table — пуст, всё делает system cron
- ✅ Snapshot VPS-A перед terminate — DD делает через Selectel panel

Подробнее см. `notes/technical/sprint-7-architecture-simplification.md`.
