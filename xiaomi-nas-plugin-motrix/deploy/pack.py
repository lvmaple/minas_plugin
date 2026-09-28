from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parent.parent
target = root / "motrix-plugin.zip"
with ZipFile(target, "w", ZIP_DEFLATED) as archive:
    for name in ("INFO", "src", "scripts", "deploy", "licenses"):
        path = root / name
        items = [path] if path.is_file() else path.rglob("*")
        for item in items:
            if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc":
                archive.write(item, item.relative_to(root).as_posix())
print(target)
