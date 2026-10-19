# Changelog

## 1.3.0

- Add optional long-paper support for documents up to 1828.8 mm (72 in). The
  brscan5 SANE frontend exposes only the normal 355.6 mm (14 in) height; a
  bundled preload shim enables the DS-640 firmware's simplex long-paper mode
  for scan sessions. The sensor worker and normal scan path are unchanged.
- Add `long_paper`, `long_paper_paper_type`, `long_paper_width_mm`, and
  `max_document_length_mm` settings. `long_paper_paper_type` 23 is narrow
  (107.95 mm) and 24 is the full 215.9 mm scanner width.
- Trim the scanner's uniform background from full-width long-paper images
  before writing the PDF, so narrow receipts produce tight pages.
- Install and build `native/long_paper_shim.c`; keep the vendor driver binary
  untouched. Long-paper mode is limited to 300 DPI and simplex scanning.
- Raise the default `scan_timeout_seconds` to 180 so a full 72 in page has
  enough time to complete.
- Add unit coverage for long-paper configuration, preload environment, scan
  height, and background trimming.

Validation: 33 unit tests; a live DS-640 receipt 15.5 in long was captured at
300 DPI as a 981 x 4650 image, trimmed to 3.27 x 15.5 in, and saved as a PDF
through the installed service with exit 0.

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
