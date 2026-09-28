#!/bin/sh
# dockerctl.cgi — plugin.cgi entry (forward method + body for API)
URI="$FILE_URI"
REQ="$FILE_REQ"
METHOD="${REQUEST_METHOD:-GET}"

case "$URI" in
  api/*)
    PORT="${PLUG_PORT_ALLOC:-${PLUG_PORT:-9302}}"
    URL="http://127.0.0.1:$PORT/$URI"
    case "$URL" in
      *"?"*) ;;  # URI already has query string, skip
      *) [ -n "$QUERY_STRING" ] && URL="$URL?$QUERY_STRING" ;;
    esac

    # long ops (image pull) can take minutes; keep in sync with httpd timeouts
    if [ "$METHOD" = "POST" ] || [ "$METHOD" = "PUT" ] || [ "$METHOD" = "DELETE" ]; then
      # stream request body straight to curl (up to 1MB) — no shell var, NUL-safe
      if [ -n "$CONTENT_LENGTH" ] && [ "$CONTENT_LENGTH" -gt 0 ] 2>/dev/null; then
        resp=$(head -c "$CONTENT_LENGTH" | curl -s --max-time 330 \
          -X "$METHOD" \
          -H "Content-Type: ${CONTENT_TYPE:-application/json}" \
          --data-binary @- \
          "$URL")
      else
        resp=$(curl -s --max-time 330 -X "$METHOD" "$URL")
      fi
    else
      resp=$(curl -s --max-time 330 "$URL")
    fi

    printf 'Content-type: application/json\r\n'
    printf 'Cache-Control: no-store\r\n'
    printf '\r\n'
    printf '%s' "$resp"
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
    svg)  mime="image/svg+xml" ;;
    *)    mime=$(file -b --mime-type "$REQ" 2>/dev/null || echo application/octet-stream) ;;
  esac
  printf 'Content-type: %s\r\n' "$mime"
  printf 'Cache-Control: no-cache\r\n'
  printf '\r\n'
  cat "$REQ"
else
  printf 'Status: 404 Not Found\r\n'
  printf 'Content-type: text/html\r\n'
  printf '\r\n'
  printf '<h1>404 Not Found</h1>'
fi
