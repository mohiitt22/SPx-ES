"""Stage the static dashboard with a pinned browser Python runtime."""

import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "dist" / "static"
RUNTIME = STATIC / "pyodide"
PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.29.4/full/"
CORE = ["pyodide.mjs", "pyodide.asm.js", "pyodide.asm.wasm", "python_stdlib.zip"]
PACKAGES = ["pyarrow", "scipy", "tzdata"]


def download(name, checksum=None):
    path = RUNTIME / name
    if path.is_file():
        if checksum is None or hashlib.sha256(path.read_bytes()).hexdigest() == checksum:
            return
    with urlopen(PYODIDE + name, timeout=60) as response:
        data = response.read()
    if checksum and hashlib.sha256(data).hexdigest() != checksum:
        raise ValueError(f"Integrity check failed for {name}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def dependencies(lock, names):
    selected = {}
    def visit(name):
        package = lock[name]
        selected[package["file_name"]] = package["sha256"]
        for dep in package["depends"]:
            visit(dep)
    for name in names:
        visit(name)
    return selected


def build():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    download("pyodide-lock.json")
    packages = json.loads((RUNTIME / "pyodide-lock.json").read_text())["packages"]
    files = {name: None for name in CORE} | dependencies(packages, PACKAGES)
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(lambda item: download(*item), files.items()))
    for name in [*files, "pyodide-lock.json"]:
        if (RUNTIME / name).stat().st_size >= 25 * 1024 * 1024:
            raise ValueError(f"Static artifact {name} exceeds the asset size limit.")
    for name in ("styles.css", "dashboard.js"):
        shutil.copy2(ROOT / "static" / name, STATIC / name)
    html = (ROOT / "static" / "index.html").read_text()
    html = html.replace("<body>", '<body data-calculator="browser">')
    html = html.replace('id="reset">Load example', 'id="reset" hidden>Load example')
    html = html.replace("Opening the example files…", "Choose the SPX Parquet and ES options CSV to begin.")
    html = html.replace("Local dashboard · Files are processed on this computer.",
                        'Files stay in your browser and are cleared when you reload. '
                        '<a href="./static/pyodide/THIRD_PARTY_NOTICES.txt">Open-source notices</a>')
    (ROOT / "dist" / "index.html").write_text(html)
    for name in ("browser-worker.mjs", "runtime.mjs"):
        shutil.copy2(ROOT / "hosting" / name, STATIC / name)
    for name in ("comparison.py", "inputs.py", "model.py"):
        shutil.copy2(ROOT / name, STATIC / name)
    shutil.copy2(ROOT / "hosting" / "browser.py", STATIC / "browser.py")
    for name in ("PYODIDE-LICENSE.txt", "THIRD_PARTY_NOTICES.txt"):
        shutil.copy2(ROOT / "hosting" / name, RUNTIME / name)


if __name__ == "__main__":
    build()
