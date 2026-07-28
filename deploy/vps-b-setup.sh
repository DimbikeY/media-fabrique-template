#!/usr/bin/env bash
# setup-vps-b.sh — установка WordPress + cron pipeline + FastAPI feedback
# Запускать от root один раз на чистом Ubuntu 22.04 LTS / 24.04 LTS
#
# Использование:
#   scp vps-b-setup.sh root@<VPS-B-IP>:/root/
#   ssh root@<VPS-B-IP> 'bash /root/vps-b-setup.sh'
#
# После установки:
#   - WordPress на https://<your-domain> (nginx + PHP-FPM 8.x + MariaDB)
#   - python venv /opt/<deploy-user>/.venv с media-fabrique-template/
#   - 6 cron-jobs от user <deploy-user>
#   - FastAPI feedback receiver на 127.0.0.1:8000 (через nginx proxy)
#   - SSL через Let's Encrypt
#   - fail2ban + ufw
#
# ⚠️ 2026-07-14 Sprint 7: VPS-A terminate. Этот скрипт теперь описывает
# **только** VPS-B setup, потому что VPS-A больше нет. См.
# notes/technical/sprint-7-architecture-simplification.md для текущей
# архитектуры (одна VPS, OpenClaw Gateway локально).

set -euo pipefail

echo "==> VPS-B setup: WordPress + cron pipeline + FastAPI"
echo "    Запускать на чистой Ubuntu 22.04+ или Debian 12+"

# --- 1. System update ---
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq

# --- 2. Базовые пакеты ---
apt-get install -y -qq \
    curl wget git unzip ca-certificates gnupg lsb-release \
    ufw fail2ban jq htop chrony \
    software-properties-common apt-transport-https

# --- 3. Firewall ---
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
ufw allow 80/tcp comment 'HTTP (redirect to HTTPS)'
ufw allow 443/tcp comment 'HTTPS'
# 8000/tcp закрыт снаружи — FastAPI через nginx + только 127.0.0.1
ufw --force enable
echo "==> ufw: $(ufw status | head -1)"

# --- 4. SSH hardening (аналогично VPS-A) ---
cp /etc/ssh/sshd_config /etc/ssh/sshd_config.backup-$(date +%F)
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/^#\?X11Forwarding.*/X11Forwarding no/' /etc/ssh/sshd_config
sed -i 's/^#\?MaxAuthTries.*/MaxAuthTries 3/' /etc/ssh/sshd_config
sed -i 's/^#\?AllowAgentForwarding.*/AllowAgentForwarding no/' /etc/ssh/sshd_config

# --- 4b. sysctl hardening (Sprint 6m security audit, секция P) ---
cat > /etc/sysctl.d/99-hardening.conf <<'EOF'
net.ipv4.conf.all.rp_filter=1
net.ipv4.conf.default.rp_filter=1
net.ipv4.icmp_echo_ignore_broadcasts=1
net.ipv4.conf.all.accept_redirects=0
net.ipv4.conf.default.accept_redirects=0
net.ipv6.conf.all.accept_redirects=0
net.ipv4.conf.all.send_redirects=0
net.ipv4.conf.all.accept_source_route=0
net.ipv4.conf.default.accept_source_route=0
EOF
sysctl --system >/dev/null 2>&1 || true

# --- 4c. unattended-upgrades (Sprint 6m security audit, секция M) ---
apt-get install -y -qq unattended-upgrades apt-listchanges
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";
EOF
dpkg-reconfigure -f noninteractive unattended-upgrades

# --- 4d. cron/at allowlist (Sprint 6m security audit, секция M) ---
echo "root" > /etc/cron.allow
echo "<deploy-user>" >> /etc/cron.allow
echo "ALL" > /etc/cron.deny
echo "ALL" > /etc/at.deny

# --- 5. fail2ban ---
cat > /etc/fail2ban/jail.local <<'EOF'
[DEFAULT]
bantime = 1h
findtime = 10m
maxretry = 3

[sshd]
enabled = true
port = 22
filter = sshd
logpath = /var/log/auth.log

[nginx-http-auth]
enabled = true
port = http,https
logpath = /var/log/nginx/error.log
EOF
systemctl enable --now fail2ban

# --- 5b. AppArmor enforce (Sprint 6m security audit, секция M) ---
if command -v aa-status &>/dev/null; then
    aa-status 2>/dev/null | grep -q "apparmor module is loaded" && {
        aa-enforce /etc/apparmor.d/usr/sbin.nginx 2>/dev/null || true
        aa-enforce /etc/apparmor.d/php-fpm 2>/dev/null || true
        echo "==> AppArmor: enforce mode confirmed"
    }
else
    echo "==> AppArmor not available, skipping"
fi

# --- 6. Nginx ---
apt-get install -y -qq nginx
systemctl enable --now nginx

# server_tokens off (security baseline)
echo "server_tokens off;" > /etc/nginx/conf.d/security.conf

# TLS hardening: Mozilla Intermediate, TLS 1.2+, HSTS (Sprint 6m audit, секция D)
mkdir -p /etc/nginx/snippets
cat > /etc/nginx/snippets/ssl-params.conf <<'EOF'
# TLS 1.2+ only (Mozilla Intermediate)
ssl_protocols TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;
ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;

# HSTS
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

# OCSP stapling
ssl_stapling on;
ssl_stapling_verify on;
resolver 8.8.8.8 8.8.4.4 valid=300s;
resolver_timeout 5s;
EOF

# --- 7. MariaDB ---
apt-get install -y -qq mariadb-server mariadb-client
systemctl enable --now mariadb

# mysql_secure_installation эквивалент (без интерактива)
mysql --execute="
DELETE FROM mysql.user WHERE User='';
DELETE FROM mysql.user WHERE User='root' AND Host NOT IN ('localhost', '127.0.0.1', '::1');
DROP DATABASE IF EXISTS test;
DELETE FROM mysql.db WHERE Db='test' OR Db='test\\_%';
FLUSH PRIVILEGES;
"

# Создаём БД для WP и юзера
# DD: замени пароль после установки
WP_DB_NAME="${WP_DB_NAME:-wp_media<deploy-user>}"
WP_DB_USER="${WP_DB_USER:-wp_<deploy-user>}"
WP_DB_PASS="${WP_DB_PASS:-}"  # ОБЯЗАТЕЛЬНО сгенерируй и положи в /opt/<deploy-user>/.env

if [[ -z "$WP_DB_PASS" ]]; then
    echo "==> ГЕНЕРАЦИЯ пароля WP_DB_PASS"
    WP_DB_PASS=$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)
    echo "    Сгенерированный пароль: $WP_DB_PASS"
    echo "    ЗАПОМНИ ИЛИ ЗАПИШИ В /opt/<deploy-user>/.env (DB_PASSWORD)"
fi

mysql --execute="
CREATE DATABASE IF NOT EXISTS \`${WP_DB_NAME}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${WP_DB_USER}'@'localhost' IDENTIFIED BY '${WP_DB_PASS}';
GRANT ALL PRIVILEGES ON \`${WP_DB_NAME}\`.* TO '${WP_DB_USER}'@'localhost';
FLUSH PRIVILEGES;
"

# --- 7b. Read-only mysql user (для ssh-gate запросов от VPS-A) ---
# Используется OpenClaw'ом на VPS-A через ssh + ssh-gate (Sprint 6m architecture).
# Только SELECT — никаких изменений.
RO_DB_USER="${RO_DB_USER:-ro_<deploy-user>}"
RO_DB_PASS="${RO_DB_PASS:-$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)}"
mysql --execute="
CREATE USER IF NOT EXISTS '${RO_DB_USER}'@'localhost' IDENTIFIED BY '${RO_DB_PASS}';
GRANT SELECT ON \`${WP_DB_NAME}\`.* TO '${RO_DB_USER}'@'localhost';
FLUSH PRIVILEGES;
"
echo "==> Read-only mysql user '${RO_DB_USER}@localhost' создан (SELECT only)"
echo "    Пароль: ${RO_DB_PASS}"
echo "    Положи в /opt/<deploy-user>/.env: DB_RO_PASSWORD=${RO_DB_PASS}"

# --- 8. PHP 8.x + extensions для WP ---
PHP_VERSION="${PHP_VERSION:-8.3}"
add-apt-repository -y ppa:ondrej/php
apt-get update -qq
apt-get install -y -qq \
    php${PHP_VERSION}-fpm php${PHP_VERSION}-cli php${PHP_VERSION}-common \
    php${PHP_VERSION}-mysql php${PHP_VERSION}-xml php${PHP_VERSION}-curl \
    php${PHP_VERSION}-gd php${PHP_VERSION}-imagick php${PHP_VERSION}-mbstring \
    php${PHP_VERSION}-zip php${PHP_VERSION}-intl php${PHP_VERSION}-bcmath \
    php${PHP_VERSION}-opcache

# PHP-FPM hardening
PHP_INI="/etc/php/${PHP_VERSION}/fpm/php.ini"
sed -i 's/^expose_php = On/expose_php = Off/' $PHP_INI
sed -i 's/^;cgi.fix_pathinfo=1/cgi.fix_pathinfo=0/' $PHP_INI
sed -i 's/^memory_limit = .*/memory_limit = 256M/' $PHP_INI
sed -i 's/^;opcache.enable=1/opcache.enable=1/' /etc/php/${PHP_VERSION}/fpm/conf.d/10-opcache.ini

systemctl enable --now php${PHP_VERSION}-fpm

# --- 9. WordPress ---
mkdir -p /var/www/<your-domain>
cd /tmp
if [[ ! -f /tmp/latest.tar.gz ]]; then
    wget -q https://wordpress.org/latest.tar.gz
fi
tar -xzf latest.tar.gz -C /var/www/<your-domain> --strip-components=1

# WP permissions
chown -R www-data:www-data /var/www/<your-domain>
find /var/www/<your-domain> -type d -exec chmod 755 {} \;
find /var/www/<your-domain> -type f -exec chmod 644 {} \;

# wp-config.php создаётся через wp-cli (см. ниже)

# --- 10. wp-cli ---
if ! command -v wp &>/dev/null; then
    curl -fsSL -o /usr/local/bin/wp https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar
    chmod +x /usr/local/bin/wp
fi

# --- 11. User <deploy-user> (для cron и FastAPI) ---
if ! id <deploy-user> &>/dev/null; then
    useradd -m -s /bin/bash <deploy-user>
fi
mkdir -p /opt/<deploy-user> /var/log/<project>
chown -R <deploy-user>:<deploy-user> /opt/<deploy-user> /var/log/<project>
chmod 750 /opt/<deploy-user> /var/log/<project>

# --- 12. Python venv ---
apt-get install -y -qq python3 python3-venv python3-pip
sudo -u <deploy-user> python3 -m venv /opt/<deploy-user>/.venv
sudo -u <deploy-user> /opt/<deploy-user>/.venv/bin/pip install --upgrade pip wheel setuptools

# media-fabrique-template будет склонирован руками (см. runbook)
# Заглушка для requirements.txt
cat > /opt/<deploy-user>/requirements.txt <<'EOF'
# Положи requirements.txt из media-fabrique-template/
# pip install -r requirements.txt
EOF
chown <deploy-user>:<deploy-user> /opt/<deploy-user>/requirements.txt

# --- 13. Cron-jobs setup (заготовка) ---
CRON_FILE="/etc/cron.d/<project>-pipeline"
cat > $CRON_FILE <<'EOF'
# /etc/cron.d/<project>-pipeline
# Все 6 cron-job'ов из Sprint 5 lightweight.
# Полные пути и команды — DD заполнит после git clone.
#
# SHELL=/bin/bash
# PATH=/opt/<deploy-user>/.venv/bin:/usr/local/bin:/usr/bin:/bin
# PYTHONUNBUFFERED=1
#
# */30 * * * *   <deploy-user>  cd /opt/<deploy-user> && .venv/bin/python -m media-fabrique-template.main --tick=fetch
# */7  * * * *   <deploy-user>  cd /opt/<deploy-user> && .venv/bin/python -m media-fabrique-template.main --tick=rewrite
# */5  * * * *   <deploy-user>  cd /opt/<deploy-user> && .venv/bin/python -m media-fabrique-template.main --tick=publish
# 0    * * * *   <deploy-user>  cd /opt/<deploy-user> && .venv/bin/python -m media-fabrique-template.main --tick=janitor
# 0    7 * * *   <deploy-user>  cd /opt/<deploy-user> && .venv/bin/python -m media-fabrique-template.main --tick=morning_report
# */1  * * * *   <deploy-user>  cd /opt/<deploy-user> && .venv/bin/python -m media-fabrique-template.feedback_receiver
EOF
chmod 644 $CRON_FILE
chown root:root $CRON_FILE

# --- 14a. Daily-stats + morning-report + feedback-digest cron files ---
# DD 2026-07-20 09:14 MSK: collect the three orphaned cron.d files
# (mf-daily-stats, mf-morning-report, mf-feedback-digest) into the
# setup script so a fresh VPS picks them up automatically. The
# deploy/mf-morning-report.sh script in the repo is the source of
# truth — it gets copied to /opt/<deploy-user>-ops/bin/ here, and the cron
# below references it directly.

mkdir -p /opt/<deploy-user>-ops/bin
install -m 0755 deploy/mf-morning-report.sh /opt/<deploy-user>-ops/bin/mf-morning-report.sh
install -m 0755 deploy/mf-daily-stats.sh  /opt/<deploy-user>-ops/bin/mf-daily-stats.sh

cat > /etc/cron.d/mf-daily-stats <<EOF
# /etc/cron.d/mf-daily-stats — passive JSON archive, no TG push
# 03:30 UTC = 06:30 MSK. Writes /opt/<deploy-user>/data/daily-stats/<DATE>.json.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

30 3 * * * <deploy-user> /opt/<deploy-user>-ops/bin/mf-daily-stats.sh >> /var/log/<project>/mf-daily-stats.log 2>&1
EOF
chmod 644 /etc/cron.d/mf-daily-stats

cat > /etc/cron.d/mf-morning-report <<EOF
# /etc/cron.d/mf-morning-report — human-readable daily digest
# 07:00 MSK. Pushes to #morning-report topic (TG_THREAD_MORNING_REPORT=7).
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
MAILTO=""
TZ=Europe/Moscow

0 7 * * *   <deploy-user>  /opt/<deploy-user>-ops/bin/mf-morning-report.sh >> /var/log/<project>/mf-morning.log 2>&1
EOF
chmod 644 /etc/cron.d/mf-morning-report

# Sprint cleanup 2026-07-21: feedback_digest cron removed. /feedback and
# /feedback_tg are write-only notes (no aggregation tick).

# --- 14. FastAPI feedback receiver (заготовка) ---
# DD: после git clone настроит uvicorn + systemd unit
# systemd unit — отдельный файл
cat > /etc/systemd/system/<deploy-user>-feedback.service.example <<EOF
[Unit]
Description=Fabrique Feedback Receiver (FastAPI)
After=network-online.target

[Service]
Type=simple
User=<deploy-user>
Group=<deploy-user>
WorkingDirectory=/opt/<deploy-user>
ExecStart=/opt/<deploy-user>/.venv/bin/uvicorn feedback_api:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5
EnvironmentFile=/opt/<deploy-user>/.env

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# --- 14b. ssh-gate wrapper (Sprint 6m architecture, control plane → data plane) ---
# Force-command для ssh-ключа от openclaw@VPS-A. Whitelist команд: git (safe),
# mysql SELECT, systemctl status, tail, backup.sh. Подробности в
# notes/technical/sprint-6m-architecture.md → "SSH между VPS-A и VPS-B".
mkdir -p /opt/<deploy-user>/bin /var/log/<project>
# <deploy-user>-ssh-gate.sh будет скопирован DD отдельно (см. DEPLOY-RUNBOOK.md) после git clone.
# Сейчас просто готовим директории и права.
chown root:root /opt/<deploy-user>/bin
chmod 755 /opt/<deploy-user>/bin
touch /var/log/<project>/ssh-gate.log
chown <deploy-user>:adm /var/log/<project>/ssh-gate.log
chmod 640 /var/log/<project>/ssh-gate.log
echo "==> ssh-gate: директории готовы (скрипт <deploy-user>-ssh-gate.sh — после git clone)"

# --- 15. logrotate для <deploy-user> ---
cat > /etc/logrotate.d/<project> <<'EOF'
/var/log/<project>/*.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    create 0640 <deploy-user> <deploy-user>
    sharedscripts
    postrotate
        # Если uvicorn поддерживает SIGHUP — reload
        systemctl reload <deploy-user>-feedback.service >/dev/null 2>&1 || true
    endscript
}
EOF

# --- 16. SSL через Let's Encrypt ---
apt-get install -y -qq certbot python3-certbot-nginx
# Запуск certbot — после настройки nginx site (DD выполнит отдельно)
# certbot --nginx -d <your-domain> -d www.<your-domain>

# --- 16b. (removed 2026-07-20 DD) Object Storage backup ---
# DD 2026-07-20 09:14 MSK: "Удали - <deploy-user>-backup backup.sh - у нас нет S3".
# Cron `<deploy-user>-backup`, scripts `backup.sh` + `<deploy-user>-backup-s3cfg.sh` больше
# не создаются. Если backup понадобится — gdrive-rclone restic или
# rsync netcat stdio. На момент закрытия проекта backup делается
# вручную `dd` на VPS-A в день перед её terminate.

# --- 17. Финальный отчёт ---
cat <<EOF

==> VPS-B setup завершён.

Что установлено:
  - nginx (server_tokens off, /etc/nginx/conf.d/security.conf)
  - PHP ${PHP_VERSION}-FPM + extensions
  - MariaDB (root через unix-socket, ${WP_DB_NAME} БД создана)
  - WordPress в /var/www/<your-domain> (chown www-data)
  - wp-cli в /usr/local/bin/wp
  - user <deploy-user> (UID=$(id -u <deploy-user>)), /opt/<deploy-user>, /var/log/<project>
  - python venv /opt/<deploy-user>/.venv
  - cron-jobs заглушка в /etc/cron.d/<project>-pipeline
  - certbot для Let's Encrypt
  - fail2ban, ufw

БД:
  Имя: ${WP_DB_NAME}
  User: ${WP_DB_USER}
  Pass: ${WP_DB_PASS}  ← СОХРАНИ! Положи в /opt/<deploy-user>/.env

Следующие шаги:
  1. Склонировать media-fabrique-template в /opt/<deploy-user>
     sudo -u <deploy-user> git clone https://github.com/<your-org>/media-fabrique-template.git /opt/<deploy-user>/media-fabrique-template
  2. Скопировать .env с локального Mac (НЕ из репо!)
     sudo cp /root/<deploy-user>.env /opt/<deploy-user>/.env
     sudo chown <deploy-user>:<deploy-user> /opt/<deploy-user>/.env
     sudo chmod 600 /opt/<deploy-user>/.env
  3. cd /opt/<deploy-user> && sudo -u <deploy-user> .venv/bin/pip install -r media-fabrique-template/requirements.txt
  4. wp config create + wp core install (или ручная настройка)
  5. Раскомментировать cron-jobs в /etc/cron.d/<project>-pipeline
  6. Настроить nginx site (см. /etc/nginx/sites-available/<your-domain>)
  7. certbot --nginx -d <your-domain> -d www.<your-domain>
  8. Перенести uploads: rsync из localWP /var/www/wp-content/uploads/

Логи:
  journalctl -u nginx -f
  journalctl -u php${PHP_VERSION}-fpm -f
  journalctl -u <deploy-user>-feedback -f  (после настройки)
  tail -f /var/log/<project>/*.log

EOF
