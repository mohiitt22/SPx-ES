# SPX / ES volatility comparison

Compare hourly SPX and E-mini S&P 500 options by settlement time and forward moneyness. Select an SPX Parquet file and an ES options CSV, then choose an expiry series, snapshot and strike. The optional ES futures CSV takes `timestamp,futures_contract,price` with offset-aware timestamps that match the ES snapshot slots.

The hosted calculator reads files in the browser. Quote data and results are cleared when the page reloads.

## Build locally

Run `python3 hosting/build.py`, then `python3 -m http.server --bind 127.0.0.1 --directory dist 8765`. The build downloads the pinned browser runtime and verifies the Python package checksums. GitHub Actions uses the same build for Pages.

The Flask version uses Python 3.12 with the packages in `requirements.txt`. Run `python app.py` to start it and `python -m unittest` to run the calculation tests.
