#!/bin/bash
# VPS cron から呼ぶ: 公式サイトの割増を取り直し、rates.json が変わっていれば GitHub に push（Pagesが自動反映）
set -u
cd "$(dirname "$0")"
REPO=~/apps/ddp-rates/repo
export GIT_SSH_COMMAND="ssh -i ~/.ssh/ddp_calc_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
git -C "$REPO" pull -q --rebase || exit 1
timeout 600 xvfb-run -a ~/apps/ddp-rates/.venv/bin/python "$REPO/updater/update_rates.py" "$REPO/rates.json"
rc=$?
# 値が変わった時だけコミット（checked の時刻だけの変化では push しない）
if git -C "$REPO" diff --quiet -I '"checked"' -- rates.json; then
  git -C "$REPO" checkout -q -- rates.json
  echo "no change (rc=$rc)"
else
  git -C "$REPO" add rates.json
  git -C "$REPO" -c user.name="ddp-rates bot" -c user.email="noreply@kagoya" commit -q -m "rates.json 自動更新 $(date '+%Y-%m-%d %H:%M')"
  git -C "$REPO" push -q && echo "pushed (rc=$rc)"
fi
