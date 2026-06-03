# Poisson Waiting Scripts

Local implementation for `/Users/chenboyuan/Downloads/SKILL.md`.

## Single Ticker

```bash
python3 scripts/fetch-data.py NVDA --news 5 > nvda.json
/Applications/Codex.app/Contents/Resources/node scripts/build-card.js --in nvda.json > nvda.md
```

## DSX Batch

```bash
python3 scripts/generate-dsx-reports.py --limit 3 --throttle 30
```

Yahoo Finance may return `429 Too Many Requests`. The fetcher uses throttling and exponential backoff. Batch generation refuses to write an incomplete report when core price data is unavailable; use `--allow-incomplete` only for diagnostic drafts.

To resume into an existing output directory without overwriting existing company reports:

```bash
python3 scripts/generate-dsx-reports.py --offset 1 --allow-existing --throttle 60
```
