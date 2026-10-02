#!/bin/sh
# Install or upgrade nzbget-dupe-proxy. Run as root from a checkout: sudo sh deploy/install.sh
set -eu
cd "$(dirname "$0")/.."
install -d -m 755 /opt/nzbget-dupe-proxy
install -m 644 nzbget_dupe_proxy.py /opt/nzbget-dupe-proxy/nzbget_dupe_proxy.py
install -m 644 deploy/nzbget-dupe-proxy.service /etc/systemd/system/nzbget-dupe-proxy.service
if [ ! -e /etc/nzbget-dupe-proxy.env ]; then
    install -m 600 -o root -g root .env.example /etc/nzbget-dupe-proxy.env
    echo "Created /etc/nzbget-dupe-proxy.env from the template: set HYDRA_APIKEY there."
fi
python3 -m py_compile /opt/nzbget-dupe-proxy/nzbget_dupe_proxy.py
systemctl daemon-reload
systemctl enable nzbget-dupe-proxy
systemctl restart nzbget-dupe-proxy
systemctl --no-pager --lines=5 status nzbget-dupe-proxy
