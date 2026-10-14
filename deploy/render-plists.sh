#!/usr/bin/env bash
# Render the three launchd templates for this machine and print the load commands.
# Usage:  cd deploy && ./render-plists.sh <label-prefix>
# e.g.    ./render-plists.sh com.example        -> com.example.notion-bridge, etc.
set -euo pipefail

PREFIX="${1:?usage: render-plists.sh <label-prefix>, e.g. com.example}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HERE="$REPO/deploy"
OUT="$HERE/rendered"
mkdir -p "$OUT" "$REPO/bridge/logs" "$REPO/worker/state"

for f in notion-bridge notion-tunnel miaoda-worker; do
  sed -e "s#__REPO__#$REPO#g" \
      -e "s#__HOME__#$HOME#g" \
      -e "s#__LABEL__#$PREFIX#g" \
      "$HERE/$f.plist.in" > "$OUT/$f.plist"
done

echo "rendered into $OUT — review them, then install:"
for f in notion-bridge notion-tunnel miaoda-worker; do
  cat <<EOF
  cp $OUT/$f.plist ~/Library/LaunchAgents/$PREFIX.$f.plist
  launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/$PREFIX.$f.plist
EOF
done
echo
echo "verify:  launchctl print gui/\$(id -u)/$PREFIX.notion-bridge | head"
echo "logs:    bridge/logs/*.log   worker/state/launchd*.log"
