#!/bin/sh
# pcbackup 入口脚本：api/* 转发给本地服务（支持 GET/POST/PUT 透传请求体），其余按静态文件返回
# 注意：上传大文件建议使用 deploy/nginx-location.conf 的 nginx 反代（主路径）；
#       本 cgi 代理是未配置 nginx 时的兜底，JSON 接口可用。
URI="$FILE_URI"
REQ="$FILE_REQ"
METHOD="${REQUEST_METHOD:-GET}"
QS="$QUERY_STRING"

case "$URI" in
  api/*)
    tmp="$(mktemp /tmp/pcbackup_cgi.XXXXXX)" || exit 1
    code=$(curl -s -o "$tmp" -w '%{http_code}' \
      -X "$METHOD" \
      -H "Content-Type: ${CONTENT_TYPE:-application/json}" \
      --data-binary @- --max-time 7200 \
      "http://127.0.0.1:9303/$URI${QS:+?$QS}")
    # shellcheck disable=SC2086
    echo -e "Status: $code\r"
    echo -e "Content-type: application/json\r\nAccess-Control-Allow-Origin: *\r\nCache-Control: no-store\r"
    echo ""
    cat "$tmp"
    rm -f "$tmp"
    exit 0
    ;;
esac

if [ -f "$REQ" ]; then
  ext="${REQ##*.}"
  case "$ext" in
    png)  mime="image/png" ;;
    css)  mime="text/css" ;;
    js)   mime="application/javascript" ;;
    html) mime="text/html" ;;
    json) mime="application/json" ;;
    *)    mime=$(file -b --mime-type "$REQ" 2>/dev/null) ;;
  esac
  echo -e "Content-type: $mime\r\nAccess-Control-Allow-Origin: *\r"
  # HTML 禁止任何缓存（含 App 本地代理），防止更新后功能"消失"
  case "$ext" in
    html) echo -e "Cache-Control: no-store, no-cache, must-revalidate, max-age=0\r\nPragma: no-cache\r" ;;
  esac
  echo ""
  cat "$REQ"
else
  echo -e "Status: 404 Not Found\r\nContent-type: text/html\r"
  echo ""
  echo "<h1>404 Not Found</h1>"
fi
