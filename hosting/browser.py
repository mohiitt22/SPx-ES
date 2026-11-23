"""Expose the original calculator to a browser worker through JSON messages."""

import json
from io import BytesIO

from comparison import catalog, compare
from inputs import Inputs

dataset = None


class BrowserFile(BytesIO):
    def __init__(self, payload, filename):
        super().__init__(payload)
        self.filename = filename


def receive_files(payload, names):
    global dataset
    parsed = Inputs.load(*(BrowserFile(content.to_py(), name) if name else None
                           for content, name in zip(payload, names)))
    info = catalog(parsed, {})
    dataset = parsed
    return json.dumps({"dataset": "browser", "catalog": info}, allow_nan=False)


def run_action(action, payload):
    if dataset is None:
        raise ValueError("Choose both options files before running a comparison.")
    options = json.loads(payload)
    operation = {"catalog": catalog, "compare": compare}[action]
    return json.dumps(operation(dataset, options), allow_nan=False)
