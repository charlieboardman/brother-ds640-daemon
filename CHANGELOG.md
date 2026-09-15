# Changelog

## 1.2.0

- Detect inserted paper through the bundled Brother driver's sensor query, then
  perform one normal scan with automatic document sizing. Return to waiting
  after each page. The ATCN preload shim is no longer used or built at install.
- Bound sensor and scan workers with timeouts and terminate their process groups
  on shutdown. Recheck USB discovery between workers and scans.
- After a failed scan, wait for the feeder to clear before scanning again.
- Publish only complete normal scans. Retain partial failures for investigation,
  while preserving recovery of files left by older ATCN releases.
- Default to automatic sizing at 300 DPI. Add `scan_timeout_seconds` (60 by
  default); accept legacy ATCN settings without using them.

Validation: 25 unit tests; installer syntax and dry run; live DS-640 insertion
through the installed service produced a one-page 1617 × 1254 scan at 300 DPI,
saved its PDF, and returned to waiting. A separate normal-scan feasibility test
also succeeded on another receipt. No private receipt images are in this repo.
