# Changelog

## v1.9.4
- added Page Types analysis tab with editable regex rules
- automatically summarizes URL count, average crawl depth, average unique inlinks/outlinks, 200 OK count and indexable URLs by page type
- automatically builds a page-type internal link map (Source Page Type -> Target Page Type)
- shows main incoming/outgoing page types and supports CSV export for summary, link map and classified URLs


## v1.9.3
- Include Regex crawl scope: start URL is crawled as the seed, then only matching internal URLs are queued
- dedicated SEO Spider-style CSV exports for Internal HTML, All Inlinks, All Outlinks, Canonicals, Directives, Response Codes, Page Titles, Meta Description, H1, H2 and Crawl Depth
- large link exports are generated on demand instead of during every app rerun


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
