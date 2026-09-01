#!/bin/bash
# 맥에서 10분마다 감시하도록 launchd 에 등록합니다.
#   등록:  ./install-local.sh
#   해제:  ./install-local.sh uninstall
#
# ~/Library/LaunchAgents/ 에 설정 파일 하나를 만듭니다.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.snu.wedding.watch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TARGET="gui/$(id -u)/$LABEL"

if [ "${1:-}" = "uninstall" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "감시를 해제했습니다."
  exit 0
fi

PYTHON="$(command -v python3)"
[ -n "$PYTHON" ] || { echo "python3 를 찾을 수 없습니다."; exit 1; }

[ -f "$DIR/.env" ] || { echo "먼저 .env 를 만들어 주세요:  cp .env.example .env"; exit 1; }
chmod 600 "$DIR/.env"    # 비밀번호가 들어 있으므로 본인만 읽도록 좁힌다

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$DIR/watch.py</string>
        <string>--state</string>
        <string>$DIR/state.local.json</string>
        <string>--quiet</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DIR</string>
    <key>StartInterval</key>
    <integer>600</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$DIR/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>$DIR/launchd.log</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "등록 완료. 10분마다 검사합니다."
echo
echo "  상태 확인   launchctl print $TARGET | head -20"
echo "  즉시 실행   launchctl kickstart -k $TARGET"
echo "  로그 보기   tail -f $DIR/watch.log"
echo "  해제        ./install-local.sh uninstall"
