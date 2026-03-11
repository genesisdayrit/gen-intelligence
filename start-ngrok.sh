#!/bin/sh
set -eu

if [ -z "${NGROK_DOMAIN:-}" ]; then
  echo "NGROK_DOMAIN is required" >&2
  exit 1
fi

exec ngrok http --url="$NGROK_DOMAIN" app:8000
