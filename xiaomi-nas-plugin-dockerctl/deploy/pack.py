#!/usr/bin/env python3
"""Pack the plugin into a zip with POSIX-style entry names (Compress-Archive
on Windows writes backslashes, which breaks unzip on the NAS)."""
import os
import sys
import zipfile

DEPLOY_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(DEPLOY_DIR)
WORK_DIR = os.path.dirname(PLUGIN_DIR)
OUT = os.path.join(WORK_DIR, "dockerctl-plugin.zip")
TARGETS = ["INFO", "src", "scripts", "deploy"]


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for t in TARGETS:
            p = os.path.join(PLUGIN_DIR, t)
            if os.path.isfile(p):
                z.write(p, t)
                continue
            for dp, _dirs, fns in os.walk(p):
                for fn in sorted(fns):
                    if fn.endswith((".pyc", ".bak")) or fn == "pack.py":
                        continue
                    fp = os.path.join(dp, fn)
                    arc = os.path.relpath(fp, PLUGIN_DIR).replace("\\", "/")
                    z.write(fp, arc)
    names = sorted(zipfile.ZipFile(OUT).namelist())
    print(f"packed {OUT} ({os.path.getsize(OUT)} bytes, {len(names)} files)")
    for n in names:
        print(" ", n)


if __name__ == "__main__":
    main()
