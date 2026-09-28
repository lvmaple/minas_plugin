#!/bin/sh
TOKEN_FILE=/data/motrix-server/data/operator-token
BASE=http://127.0.0.1:8080
if [ "${FILE_REQ##*/}" != motrix.cgi ] && [ -f "$FILE_REQ" ]; then
  case "${FILE_REQ##*.}" in
    html) MIME=text/html ;;
    css) MIME=text/css ;;
    js) MIME=application/javascript ;;
    json) MIME=application/json ;;
    png) MIME=image/png ;;
    ico) MIME=image/x-icon ;;
    svg) MIME=image/svg+xml ;;
    woff2) MIME=font/woff2 ;;
    *) MIME=application/octet-stream ;;
  esac
  printf 'Content-type: %s\r\nCache-Control: no-cache\r\n\r\n' "$MIME"
  cat "$FILE_REQ"
  exit 0
fi
if [ "$QUERY_STRING" = action=web ]; then
  printf 'Content-type: text/html; charset=utf-8\r\nCache-Control: no-store\r\n\r\n'
  curl -fsS --max-time 8 "$BASE/" || printf '<!doctype html><title>Motrix 页面不可用</title>'
  exit 0
fi
printf 'Content-type: application/json\r\nCache-Control: no-store\r\n\r\n'
case "$QUERY_STRING" in
  action=health|health=1|'')
    if curl -fsS --max-time 3 "$BASE/healthz" >/dev/null 2>&1; then
      printf '{"ok":true}'
    else
      printf '{"ok":false,"message":"Motrix Server 未就绪"}'
    fi ;;
  action=tasks)
    if [ ! -r "$TOKEN_FILE" ]; then printf '{"error":"Operator token 不可读"}'; exit 0; fi
    TOKEN="$(cat "$TOKEN_FILE")"
    curl -fsS --max-time 8 -H "Authorization: Bearer $TOKEN" \
      -H 'Content-Type: application/json' --data-binary '{"args":[]}' \
      "$BASE/rpc/query/query:listTasks" || printf '{"error":"任务列表暂不可用"}' ;;
  action=add)
    if [ "$REQUEST_METHOD" != POST ] || [ ! -r "$TOKEN_FILE" ]; then
      printf '{"error":"请求无效或 Operator token 不可读"}'; exit 0
    fi
    case "$CONTENT_LENGTH" in ''|*[!0-9]*) printf '{"error":"请求大小无效"}'; exit 0;; esac
    if [ "$CONTENT_LENGTH" -gt 8192 ]; then printf '{"error":"请求过大"}'; exit 0; fi
    TOKEN="$(cat "$TOKEN_FILE")"
    head -c "$CONTENT_LENGTH" | curl -fsS --max-time 15 \
      -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      --data-binary @- "$BASE/rpc/command/command:createTask" \
      || printf '{"error":"新建任务失败"}' ;;
  *) printf '{"error":"未知操作"}' ;;
esac
