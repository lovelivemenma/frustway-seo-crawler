# Security Notes

## Browser Release v1.9

Implemented application-level protections:

- HTTP / HTTPS only
- rejects localhost
- rejects `.local`, `.internal`, `.localhost`
- rejects non-global IP addresses
- rejects URLs containing username/password
- rejects non-standard ports in Browser mode
- validates DNS resolution before requests
- isolates crawl output by Job ID
- limits Browser crawls to 5,000 URLs by default
- limits concurrent crawls to 2 by default

## Important limitation

Application-layer hostname validation does not replace infrastructure-level network isolation.

For a stronger production deployment:

- deny access from crawler workers to cloud metadata endpoints
- deny private/internal network egress
- run crawler workers in an isolated network
- add authentication / rate limiting
- move jobs from local ephemeral disk to durable storage
