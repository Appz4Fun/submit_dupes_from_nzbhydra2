#!/bin/sh
# Copy the service code into nzbget_extension/ (the extension must be self-contained in nzbget's ScriptDir).
set -e
cd "$(dirname "$0")/.."
cp nzbget_dupe_proxy.py donor_health.py nzbget_extension/
rm -rf nzbget_extension/vendor
cp -R vendor nzbget_extension/vendor
find nzbget_extension -name __pycache__ -prune -exec rm -rf {} +
