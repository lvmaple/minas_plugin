#!/bin/sh
# sysmon.cgi — plugin.cgi 入口（协议对齐 ipc.cgi）
# api/* → 本地服务 127.0.0.1:9301；其余 → 静态文件

# 不允许直接请求 cgi 自身
[ "$FILE_URI" = "sysmon.cgi" ] && {
    printf "Status: 404 Not Found\r\nContent-type: text/html\r\n\r\n"
    echo "<html><body><h1>404 Not Found</h1></body></html>"
    exit 0
}

# 1. API 请求：转发到本地服务
case "$FILE_URI" in
    api/*)
        resp=$(curl -s --max-time 5 "http://127.0.0.1:9301/$FILE_URI")
        printf "Content-type: application/json\r\n"
        printf "Access-Control-Allow-Origin: *\r\n"
        printf "Cache-Control: no-store\r\n"
        printf "\r\n"
        printf "%s" "$resp"
        exit 0
        ;;
esac

# 2. 静态文件
if [ -f "$FILE_REQ" ]; then
    mime=""
    ext="${FILE_REQ##*.}"
    case "$ext" in
        jpg|jpeg) mime="image/jpeg" ;;
        png)     mime="image/png" ;;
        ico)     mime="image/x-icon" ;;
        svg)     mime="image/svg+xml" ;;
        css)     mime="text/css" ;;
        js)      mime="application/javascript" ;;
        html|htm) mime="text/html" ;;
        json)    mime="application/json" ;;
        txt)     mime="text/plain" ;;
        *)       mime="$(file -b --mime-type "$FILE_REQ")" ;;
    esac
    printf "Content-type: %s\r\n" "$mime"
    printf "\r\n"
    cat "$FILE_REQ"
    exit 0
fi

# 3. 404
printf "Status: 404 Not Found\r\nContent-type: text/html\r\n\r\n"
echo "<html><body><h1>404 Not Found</h1></body></html>"
