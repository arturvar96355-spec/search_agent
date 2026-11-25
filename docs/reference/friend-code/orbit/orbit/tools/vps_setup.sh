#!/bin/bash
# Настройка сервера под пульт. Запускается НА сервере от root, один раз:
#
#   ssh -i ~/.ssh/orbit_tunnel root@IP 'bash -s' < tools/vps_setup.sh pult.uniqore.ru
#
# Что делает сервер: принимает с мака SSH-канал и отдаёт через него пульт
# по https. Данных на нём нет никаких — все базы остаются на маке, поэтому
# сервер можно снести и поднять заново за несколько минут.
#
# Повторный запуск безопасен: каждый шаг сначала проверяет, сделан ли он.
set -euo pipefail

DOMAIN="${1:?Укажи домен: vps_setup.sh pult.uniqore.ru}"
# Порт на сервере, куда приходит канал с мака. Слушает только 127.0.0.1:
# снаружи его не видно, до него добирается лишь nginx на этой же машине.
PORT=18137
TUNNEL_KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIGtRyYoGjRk5dR8k0f+Hgx+Mbbf6bmiEkmPFlcPtf3v orbit-vps-tunnel'

echo "== пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx certbot python3-certbot-nginx ufw >/dev/null

echo "== пользователь для канала"
# Отдельный пользователь без оболочки. Ключ ему выдан с ограничением: можно
# ТОЛЬКО пробросить один порт на 127.0.0.1:$PORT. Ни команд, ни файлов, ни
# других портов. Утечёт этот ключ — с ним нельзя сделать ничего, кроме
# подмены пульта, и это видно сразу.
id tunnel >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin tunnel
install -d -m 700 -o tunnel -g tunnel /home/tunnel/.ssh
echo "restrict,port-forwarding,permitlisten=\"127.0.0.1:$PORT\",command=\"/bin/false\" $TUNNEL_KEY" \
  > /home/tunnel/.ssh/authorized_keys
chown tunnel:tunnel /home/tunnel/.ssh/authorized_keys
chmod 600 /home/tunnel/.ssh/authorized_keys

echo "== ssh: только ключи"
# Пароль выключен ещё при создании, но проверяем сами: образ мог включить.
cat > /etc/ssh/sshd_config.d/10-orbit.conf <<CONF
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
# Канал держится долго; без проверки живости мёртвые соединения висели бы
# часами и держали порт — новый канал с мака не смог бы его занять.
ClientAliveInterval 20
ClientAliveCountMax 3
CONF
systemctl reload ssh 2>/dev/null || systemctl reload sshd

echo "== nginx"
cat > /etc/nginx/sites-available/orbit <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        # Мак не на связи — отвечаем быстро и честно, а не висим минуту:
        # в Telegram висящая страница выглядит как сломанный пульт.
        proxy_connect_timeout 5s;
        proxy_read_timeout 60s;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/orbit /etc/nginx/sites-enabled/orbit
rm -f /etc/nginx/sites-enabled/default
nginx -t -q
systemctl reload nginx

echo "== файрвол"
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "== сертификат"
# Без почты: Let's Encrypt пишет туда только о скором истечении, а продление
# certbot делает сам. Лишний адрес в чужой базе незачем.
if ! certbot certificates 2>/dev/null | grep -q "Domains: $DOMAIN"; then
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
          --register-unsafely-without-email --redirect
fi

echo
echo "✅ Сервер готов: https://$DOMAIN"
echo "   Канал с мака: ssh -N -R 127.0.0.1:$PORT:127.0.0.1:8137 tunnel@<этот сервер>"
