#!/bin/sh
# pcbackup 服务控制脚本（start/stop/status/restart）
PORT="${PLUG_PORT:-9303}"
PIDFILE="/tmp/pcbackup/server.pid"
LOGDIR="/tmp/pcbackup"
HTTPD="${PLUG_HTTPD:-$(dirname "$0")/httpd.py}"

start() {
  mkdir -p "$LOGDIR"
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "already running"; return 0
  fi
  PLUG_PORT="$PORT" nohup /usr/bin/python3 "$HTTPD" >>"$LOGDIR/server.log" 2>&1 &
  echo $! > "$PIDFILE"
  echo "started"
}

stop() {
  [ -f "$PIDFILE" ] && { kill "$(cat "$PIDFILE")" 2>/dev/null; rm -f "$PIDFILE"; }
  echo "stopped"
}

status() {
  if { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; } || \
     systemctl is-active --quiet pcbackup.service 2>/dev/null; then
    echo "running"; exit 0
  else
    echo "stopped"; exit 1
  fi
}

restart() { stop; sleep 1; start; }

case "$1" in
  start|stop|status|restart) "$1" ;;
  *) echo "Usage: $0 {start|stop|status|restart}"; exit 1 ;;
esac
