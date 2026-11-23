"""Local web entry point. Quotes stay in this process and are cleared on restart."""

import argparse
import os
from collections import OrderedDict
from pathlib import Path
from secrets import token_urlsafe
from threading import Lock
from zoneinfo import ZoneInfoNotFoundError

from flask import Flask, jsonify, request, send_from_directory
from pyarrow import ArrowException

from comparison import catalog, compare
from inputs import Inputs

ROOT = Path(__file__).resolve().parent
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 75 * 1024 * 1024
datasets = OrderedDict()
dataset_lock = Lock()
sample = None


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.errorhandler(413)
def too_large(_error):
    return jsonify(error="Uploads must be at most 75 MB combined."), 413


@app.errorhandler(ValueError)
@app.errorhandler(ZoneInfoNotFoundError)
@app.errorhandler(ArrowException)
def invalid(error):
    return jsonify(error=str(error)), 400


@app.get("/api/sample")
def sample_info():
    if sample is None:
        return jsonify(error="The original sample files are unavailable. Upload a pair of files to start."), 404
    return jsonify(dataset="sample", catalog=catalog(sample, {}))


@app.post("/api/upload")
def upload():
    spx = request.files.get("spx")
    es = request.files.get("es")
    futures = request.files.get("futures")
    if not spx or not es:
        raise ValueError("Select an SPX Parquet and an ES options CSV.")
    data = Inputs.load(spx, es, futures if futures else None)
    info = catalog(data, {})
    token = token_urlsafe(15)
    with dataset_lock:
        datasets[token] = data
        while len(datasets) > 6:
            datasets.popitem(last=False)
    return jsonify(dataset=token, catalog=info)


def get_dataset(token):
    if token == "sample" and sample:
        return sample
    with dataset_lock:
        if token in datasets:
            datasets.move_to_end(token)
            return datasets[token]
    raise ValueError("This upload has expired. Reload the example or select the source files again.")


@app.post("/api/catalog")
def get_catalog():
    options = request.get_json()
    return jsonify(catalog(get_dataset(options.get("dataset")), options))


@app.post("/api/compare")
def get_comparison():
    options = request.get_json()
    return jsonify(compare(get_dataset(options.get("dataset")), options))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SPX / ES local volatility dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--spx", type=Path, default=Path.home() / "Desktop" / "2026-11-19.parquet")
    parser.add_argument("--es", type=Path, default=ROOT.parent / "hour" / "2026-11-19.csv")
    args = parser.parse_args()
    if args.spx.is_file() and args.es.is_file():
        try:
            sample = Inputs.load(args.spx, args.es)
        except (ValueError, OSError, ArrowException) as error:
            print(f"Example files could not be loaded: {error}", flush=True)
    os.environ.setdefault("FLASK_DEBUG", "0")
    app.run(host="127.0.0.1", port=args.port, debug=False)
