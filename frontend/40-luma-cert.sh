#!/bin/sh
# Runs when the nginx container starts. Makes a self-signed certificate once (kept in the mounted /certs folder).
# LUMA_HOSTS = comma-separated IP addresses and host names the site is opened with, e.g. 172.15.17.92,luma.tuf.edu.pk
set -e
mkdir -p /certs
if [ -s /certs/luma.crt ] && [ -s /certs/luma.key ]; then
  echo "luma: using the existing certificate in /certs"
  exit 0
fi
san=""
for h in $(echo "${LUMA_HOSTS:-localhost,127.0.0.1}" | tr ',' ' '); do
  if echo "$h" | grep -Eq '^[0-9.]+$'; then san="$san,IP:$h"; else san="$san,DNS:$h"; fi
done
san="${san#,}"
openssl req -x509 -newkey rsa:2048 -nodes -days 825 -keyout /certs/luma.key -out /certs/luma.crt \
  -subj "/O=LUMA/CN=luma" -addext "subjectAltName=$san" >/dev/null 2>&1
chmod 600 /certs/luma.key
echo "luma: created a self-signed certificate for $san"
