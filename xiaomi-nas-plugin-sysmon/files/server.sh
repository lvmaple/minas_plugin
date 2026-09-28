#!/bin/sh
# sysmon 服务控制脚本 — 供 scripts/control 与 systemd 委托调用
PLUGIN_NAME="sysmon"
PORT="${PLUG_PORT:-9301}"
PIDFILE="/tmp/${PLUGIN_NAME}/server.pid"
LOGFILE="/tmp/${PLUGIN_NAME}/server.log"
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
HTTPD="${BASE_DIR}/httpd.py"

mkdir -p "/tmp/${PLUGIN_NAME}"

start() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "already running"
    return 0
  fi
  nohup python3 "$HTTPD" >>"$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  echo "started"
}

stop() {
  if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null
    rm -f "$PIDFILE"
  fi
  echo "stopped"
}

status() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "running"
    return 0
  fi
  echo "stopped"
  return 1
}

restart() {
  stop
  start
}

case "$1" in
  start|stop|status|restart) "$1" ;;
  *)
    echo "Usage: $0 {start|stop|status|restart}"
    exit 1
    ;;
esac
