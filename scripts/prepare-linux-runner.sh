#!/usr/bin/env bash
# Keep the job's nginx service separate from a provider's Actions proxy.
set -euo pipefail
[[ $(uname -s) = Linux ]] || exit 0
[[ -e /etc/nginx/sites-enabled/github-actions-proxy ]] || exit 0
if [[ $EUID != 0 ]]; then
  exec sudo bash "$0"
fi
if systemctl is-active --quiet nginx; then
  echo 'Refusing to replace the configuration of an active nginx service.' >&2
  exit 1
fi
mkdir -p /etc/systemd/system/nginx.service.d
cat > /etc/nginx/runner-web.conf <<'NGINX'
user www-data;
worker_processes auto;
pid /run/nginx-runner-web.pid;
error_log /var/log/nginx/error.log;
include /etc/nginx/modules-enabled/*.conf;
events { worker_connections 768; }
http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    sendfile on;
    access_log /var/log/nginx/access.log;
    include /etc/nginx/sites-available/default;
}
NGINX
cat > /etc/systemd/system/nginx.service.d/runner-web.conf <<'UNIT'
[Service]
PIDFile=/run/nginx-runner-web.pid
ExecStartPre=
ExecStartPre=/usr/sbin/nginx -t -q -c /etc/nginx/runner-web.conf -g 'daemon on; master_process on;'
ExecStart=
ExecStart=/usr/sbin/nginx -c /etc/nginx/runner-web.conf -g 'daemon on; master_process on;'
ExecReload=
ExecReload=/usr/sbin/nginx -c /etc/nginx/runner-web.conf -g 'daemon on; master_process on;' -s reload
ExecStop=
ExecStop=/bin/kill -s QUIT $MAINPID
UNIT
systemctl daemon-reload
echo 'Isolated the job nginx service; the provider Actions proxy is unchanged.'
