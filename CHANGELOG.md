# Changelog

## v1.9.2
- optional crawl depth limit (Depth 0-N)
- default Polite Crawl Mode with 1-second request interval
- automatic backoff for 429/502/503/504 responses
- Browser default concurrent crawl limit reduced to 1


## v1.9
- Browser Release hardening
- per-Job SQLite / CSV isolation
- Job ID persistence through query parameters
- 24h stale Job cleanup
- public SSRF mitigation
- 5,000 URL Browser limit
- concurrent crawl limiter
- public storage restrictions
- GitHub / Streamlit deployment package

## v1.8.x
- Standard Mode scaling toward 50,000 URLs
- lazy internal-link queries
- SQLite tuning
- Site Structure parent_url optimization
- automatic partial-result handling

## v1.7.x
- crawl progress
- Stop Crawl
- result-state persistence
