#!/data/data/com.termux/files/usr/bin/bash
# 续 wake-lock, 防系统休眠掐网络。
# 独立成脚本是为了给 supervisor 一个"独占"的判活特征串:
#   pgrep -f "run-wakelock.sh"  ——  不会像 "sleep 3600" 那样撞上别人的进程。
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

termux-wake-lock 2>/dev/null

# 睡着不动就行, 进程活着即代表 wake-lock 持有中。
# 每小时重新申请一次, 防某些 ROM 悄悄回收。
while true; do
  sleep 3600
  termux-wake-lock 2>/dev/null
done
