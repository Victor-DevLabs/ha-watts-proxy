#!/bin/sh
set -e
mkdir -p /share/watts_proxy
exec python3 -u /app/proxy.py
