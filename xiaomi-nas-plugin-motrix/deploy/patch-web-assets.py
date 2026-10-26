#!/usr/bin/env python3
"""Make the official Motrix Web build work below the NAS plugin URL."""

from pathlib import Path
from decimal import Decimal
import hashlib
import re
import sys


root = Path(sys.argv[1])
uid = sys.argv[2]
if not uid.isdecimal():
    raise SystemExit("UID must be numeric")
ws_ticket = sys.argv[3]
if not re.fullmatch(r"[0-9a-f]{64}", ws_ticket):
    raise SystemExit("WebSocket ticket must be 64 lowercase hex characters")
prefix = f"/plugin/{uid}/motrix"
# Existing installs retain generated chunk names when files are copied over
# them. Only patch the upstream asset set, not prior generated outputs.
javascript_files = [
    path for path in root.glob("assets/*.js")
    if not path.name.startswith(("window-chrome-motrix-", "ws-hook"))
]
files = [root / "index.html", *javascript_files, *root.glob("assets/*.css")]
socket_chunk = "window-chrome-CZCTzY6z.js"
socket_path = root / "assets" / socket_chunk
if not socket_path.is_file():
    raise SystemExit("Motrix assets are already patched or incomplete; copy the original web assets before patching")
patched = {}
origin_marker = "globalThis.location?.origin??``"
origin_replacements = 0
socket_marker = "let e=`${this.baseUrl.replace(/^http/,`ws`)}/rpc/events`"
socket_replacements = 0
root_marker = "document.getElementById(`root`)"
root_replacements = 0
rem_pattern = re.compile(r"(?<![A-Za-z0-9_])(-?(?:\d+(?:\.\d+)?|\.\d+))rem")
rem_replacements = 0
utility_layer_replacements = 0
menu_background_replacements = 0
toolbar_tooltip_replacements = 0
locale_bootstrap_replacements = 0
locale_save_replacements = 0
contains_guard_replacements = 0
clipboard_copy_replacements = 0
task_files_fallback_replacements = 0


def unwrap_utility_layer(css):
    """Keep Motrix utilities above unlayered styles injected by the NAS host."""
    marker = "@layer utilities{"
    if css.count(marker) != 1:
        raise SystemExit(f"Expected one Motrix utilities layer, found {css.count(marker)}")
    start = css.index(marker)
    body_start = start + len(marker)
    depth = 1
    quote = None
    comment = False
    i = body_start
    while i < len(css):
        char = css[i]
        next_char = css[i + 1] if i + 1 < len(css) else ""
        if comment:
            if char == "*" and next_char == "/":
                comment = False
                i += 2
                continue
        elif quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
        elif char == "/" and next_char == "*":
            comment = True
            i += 2
            continue
        elif char == "\\":
            i += 2
            continue
        elif char in ("'", '"'):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                # Place utilities before Motrix's own unlayered component rules,
                # so those rules keep their intended priority.
                return css[body_start:i] + css[:start] + css[i + 1 :]
        i += 1
    raise SystemExit("Motrix utilities layer has no closing brace")


def rem_to_px(match):
    pixels = Decimal(match.group(1)) * 16
    return f"{format(pixels.normalize(), 'f')}px"

for path in files:
    content = path.read_text(encoding="utf-8")
    if path.suffix == ".css" and "@layer utilities{" in content:
        content = unwrap_utility_layer(content)
        utility_layer_replacements += 1
    if origin_marker in content:
        origin_replacements += content.count(origin_marker)
        content = content.replace(
            origin_marker,
            f"`{prefix}/api/{ws_ticket}`",
        )
    if socket_marker in content:
        socket_replacements += content.count(socket_marker)
        content = content.replace(
            socket_marker,
            f"let e=`${{document.getElementById(`motrix-plugin-origin`)?.getAttribute(`content`)??globalThis.location?.origin??``}}${{this.baseUrl}}/rpc/events?token={ws_ticket}`.replace(/^http/,`ws`)",
        )
    if root_marker in content:
        root_replacements += content.count(root_marker)
        content = content.replace(root_marker, "document.getElementById(`motrix-plugin-root`)")
    if path.suffix == ".js":
        # The web build normally waits for a connection-change event before
        # applying the saved locale. The NAS plugin proxy can establish RPC
        # without delivering that event, leaving the interface in English.
        locale_bootstrap_marker = "return k.platform!==`web`&&o(),()=>{t=!1,n+=1,k.off(Fr.LocaleChanged,a),s?.()}"
        locale_bootstrap_replacements += content.count(locale_bootstrap_marker)
        content = content.replace(
            locale_bootstrap_marker,
            "return o(),()=>{t=!1,n+=1,k.off(Fr.LocaleChanged,a),s?.()}",
        )
        # The NAS host portals Motrix windows into its own document. DOM nodes
        # then come from a different realm than the script, so
        # ancestor.contains(node) throws "parameter 1 is not of type 'Node'"
        # (breaks e.g. the download-history copy-link menu). Guard the walk.
        contains_marker = (
            "function sS(e,t){let n=e.parentElement;"
            "for(;n&&!n.contains(t);)n=n.parentElement;return n}"
        )
        contains_guard = (
            "function sS(e,t){if(!(t instanceof Node))return null;"
            "let n=e.parentElement;for(;n;){try{if(n.contains(t))return n}"
            "catch(r){return null}n=n.parentElement}return null}"
        )
        contains_guard_replacements += content.count(contains_marker)
        content = content.replace(contains_marker, contains_guard)
        # The NAS WebView/micro-app blocks navigator.clipboard.writeText
        # (Permissions-Policy / sandbox), so copy-link and copy-path die with
        # copyUrlFailed. Route every write through a helper that falls back to
        # a hidden textarea + document.execCommand('copy').
        clipboard_marker = "navigator.clipboard.writeText"
        clipboard_helper = (
            "globalThis.__motrixCopyText=async t=>{"
            "try{const c=navigator.clipboard;if(c&&c.writeText){await c.writeText(t);return}}"
            "catch(e){}"
            "const a=document.createElement('textarea');a.value=t;"
            "a.setAttribute('readonly','');"
            "a.style.cssText='position:fixed;left:-9999px;top:0';"
            "document.body.appendChild(a);a.focus();a.select();"
            "a.setSelectionRange(0,a.value.length);"
            "try{document.execCommand('copy')}"
            "finally{a.remove()}}"
        )
        if clipboard_marker in content:
            clipboard_copy_replacements += content.count(clipboard_marker)
            if "__motrixCopyText" not in content:
                content = clipboard_helper + ";" + content
            content = content.replace(clipboard_marker, "globalThis.__motrixCopyText")
        # Completed HTTP tasks drop out of aria2's file list after finish, so
        # GetTaskFiles returns [] and the inspector file pane shows "0 files"
        # even though finalPath still points at the downloaded blob. Synthesize
        # one row from the task record when the engine list is empty.
        task_files_marker = (
            "{files:a}=GB(e.id,i),o=e.bt?.selectedFiles??[]"
        )
        # Show each file as the Files-app-relative NAS path (我的数据/...) so the
        # inspector lists where the blob actually landed, not just basename.
        task_files_fallback = (
            "{files:_tf}=GB(e.id,i),"
            "a=(_tf&&_tf.length?_tf:"
            "(e.finalName||e.filename?"
            "[{index:0,path:e.finalName||e.filename,"
            "size:e.sizeWhenDone||e.totalBytes||0,selected:!0,"
            "completedBytes:e.downloadedBytes||0}]:_tf||[]))"
            ".map(f=>{"
            "let p=String(f&&f.path||'').replace(/\\\\/g,'/');"
            "let root=String(e.saveDir||'').replace(/\\\\/g,'/').replace(/\\/$/,'');"
            "let abs=p.startsWith('/')?p:(root?root+'/'+p:p);"
            "let m=abs.match(/\\/nas\\/pool0\\/u\\d+\\/data\\/(.+)$/);"
            "let rel=m?m[1]:p.replace(/^\\//,'');"
            "return Object.assign({},f,{path:'我的数据/'+rel});"
            "}),"
            "o=e.bt?.selectedFiles??[]"
        )
        task_files_fallback_replacements += content.count(task_files_marker)
        content = content.replace(task_files_marker, task_files_fallback)
        # Applying the chosen language directly after a successful save also
        # works when the proxy does not deliver LocaleChanged over WebSocket.
        locale_save_marker = "await k.invoke(ft.UpdateSettings,{app:n}),n.theme!==void 0&&a(n.theme),t()"
        locale_save_replacements += content.count(locale_save_marker)
        content = content.replace(
            locale_save_marker,
            "await k.invoke(ft.UpdateSettings,{app:n}),n.language!==void 0&&await xn(n.language),n.theme!==void 0&&a(n.theme),t()",
        )
        menu_marker = "className:`application-menu app-no-drag`"
        menu_background_replacements += content.count(menu_marker)
        content = content.replace(
            menu_marker,
            "className:`application-menu app-no-drag`,style:{backgroundColor:`#f5f5f5`}",
        )
        # These buttons sit at the top of a clipped NAS plugin window.
        for label in ("chrome.toggleSidebar", "chrome.newTask"):
            tooltip_marker = f"(0,j.jsx)(b_,{{children:t(`{label}`)}})"
            toolbar_tooltip_replacements += content.count(tooltip_marker)
            content = content.replace(
                tooltip_marker,
                f"(0,j.jsx)(b_,{{side:`bottom`,children:t(`{label}`)}})",
            )
    if path.suffix in (".js", ".css"):
        content, count = rem_pattern.subn(rem_to_px, content)
        rem_replacements += count
    for old in ("/assets/", "/favicon.ico", "/apple-touch-icon.png", "/app-icon.png", "/mo-logo.svg", "/xiaomi-theme.css"):
        content = content.replace(old, prefix + old)
    patched[path] = content

# The NAS app keeps its JavaScript realm alive when a plugin window closes.
# Give this patched chunk a content-derived URL so a reopened plugin cannot
# reuse the old module instance from that realm's module map.
socket_digest = hashlib.sha256(patched[socket_path].encode("utf-8")).hexdigest()[:12]
socket_new_name = f"window-chrome-motrix-{socket_digest}.js"
chunk_reference_replacements = 0
for path in files:
    if path == socket_path:
        continue
    content = patched[path]
    chunk_reference_replacements += content.count(socket_chunk)
    patched[path] = content.replace(socket_chunk, socket_new_name)

if origin_replacements != 3:
    raise SystemExit(f"Expected three Motrix API origins, found {origin_replacements}")
if socket_replacements != 1:
    raise SystemExit(f"Expected one Motrix WebSocket URL, found {socket_replacements}")
if chunk_reference_replacements != 5:
    raise SystemExit(f"Expected five WebSocket module references, found {chunk_reference_replacements}")
if root_replacements != 1:
    raise SystemExit(f"Expected one Motrix root lookup, found {root_replacements}")
if rem_replacements < 100:
    raise SystemExit(f"Expected Motrix rem units, found {rem_replacements}")
if utility_layer_replacements != 1:
    raise SystemExit(f"Expected one Motrix utilities stylesheet, found {utility_layer_replacements}")
if menu_background_replacements != 2:
    raise SystemExit(f"Expected two application menus, found {menu_background_replacements}")
if toolbar_tooltip_replacements != 2:
    raise SystemExit(f"Expected two toolbar tooltips, found {toolbar_tooltip_replacements}")
if locale_bootstrap_replacements != 1:
    raise SystemExit(f"Expected one locale bootstrap hook, found {locale_bootstrap_replacements}")
if contains_guard_replacements != 1:
    raise SystemExit(f"Expected one sS contains walk, found {contains_guard_replacements}")
if clipboard_copy_replacements != 5:
    raise SystemExit(f"Expected five clipboard writeText calls, found {clipboard_copy_replacements}")
if task_files_fallback_replacements != 1:
    raise SystemExit(f"Expected one GetTaskFiles hook, found {task_files_fallback_replacements}")
if locale_save_replacements != 1:
    raise SystemExit(f"Expected one locale save hook, found {locale_save_replacements}")

# Validate the complete transformation before touching the supplied directory.
# A failed check or an accidental repeat must leave working assets intact.
for path, content in patched.items():
    path.write_text(content, encoding="utf-8", newline="\n")
socket_path.replace(socket_path.with_name(socket_new_name))
