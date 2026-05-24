#!/usr/bin/env bash
# Music4Life — chạy web UI tải nhạc (chế độ nền, không bị IDE/Ctrl+C ngắt).
#   ./run.sh        -> khởi động nền + mở trình duyệt
#   ./run.sh stop   -> dừng server
#   ./run.sh log    -> xem log realtime
cd "$(dirname "$0")"

URL="http://127.0.0.1:8000"
LOG="$PWD/.server.log"
PIDFILE="$PWD/.server.pid"

stop() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")" && echo "⏹  Đã dừng (pid $(cat "$PIDFILE"))."
  elif lsof -ti :8000 >/dev/null 2>&1; then
    lsof -ti :8000 | xargs kill 2>/dev/null && echo "⏹  Đã dừng (theo port 8000)."
  else
    echo "Không có server nào đang chạy."
  fi
  rm -f "$PIDFILE"
}

case "$1" in
  stop) stop; exit 0 ;;
  log)  exec tail -f "$LOG" ;;
esac

# Đã chạy sẵn?
if lsof -ti :8000 >/dev/null 2>&1; then
  echo "✓ Server đã chạy sẵn tại $URL"
  command -v open >/dev/null && open "$URL"
  exit 0
fi

# Khởi động nền (nohup: bỏ qua SIGHUP; chạy & nên ^C ở prompt không đụng tới)
nohup .venv/bin/python app.py > "$LOG" 2>&1 &
echo $! > "$PIDFILE"

# Chờ server sẵn sàng (tối đa ~9s)
for _ in $(seq 1 30); do
  curl -s -o /dev/null "$URL" 2>/dev/null && break || sleep 0.3
done

if curl -s -o /dev/null "$URL" 2>/dev/null; then
  command -v open >/dev/null && open "$URL"
  echo "▶  Music4Life đang chạy nền tại $URL"
  echo "   • Dừng:     ./run.sh stop"
  echo "   • Xem log:  ./run.sh log"
else
  echo "✗ Server không lên được. Xem log: $LOG"
  tail -20 "$LOG"
  exit 1
fi
