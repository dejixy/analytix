# Dune queries for the Analyzer

Each `.sql` file here is one saved query. To use one:

1. On dune.com, create a new query, paste the file's contents, run it once, and save it.
2. Copy the number in the query's address (`dune.com/queries/1234567` → `1234567`).
3. Add the entry shown at the top of the file to `/opt/analytix/data/analyzer_dune.json` (a JSON list), with that number as `id`.
4. Restart the app, or wait for the hourly Dune pass.

`refresh_h` makes the app ask Dune to run the query again when its result is older than that many hours. Each run
spends Dune credits (the free plan has a monthly allowance), so keep it at 6 or more unless you need it fresher.

Column names decide how a column is shown: `…_usd` as dollars, `…_ton` as TON, `…_pct` or `…_change` as a signed
percentage, and `…address`, `…_item`, `buyer`, `seller` as a shortened address linked to the entry's `explorer`.
