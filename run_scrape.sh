#!/usr/bin/env bash
# Daily zahcomputers.pk scrape — triggered by zah-scraper.timer (systemd).
# View output:  journalctl -u zah-scraper.service [-f]
#
# Lives on the VPS rather than GitHub Actions because Cloudflare's cf_clearance
# cookie is bound to the IP that solved the challenge. The local FlareSolverr
# instance shares this VPS's IP (46.224.84.244); a GitHub Actions runner does
# not, so cookies returned to the runner are 403'd by Cloudflare immediately.
set -u
cd /root/megacomputer-automation-scrapping/zahcomputers-scrapping || exit 1
PY=.venv/bin/python
echo "==== zahcomputers scrape started $(date -u +%FT%TZ) ===="
$PY -m src.scraper
status=$?
echo "==== zahcomputers scrape finished (exit $status) $(date -u +%FT%TZ) ===="
exit $status
