---
name: contract-redline-review
description: Compare agreement versions, flag risk deltas, and produce bounded redline workpapers.
modes: [legal, ga-ca]
---

## Contract Redline Review
1. Parse baseline and revised versions via `keel.docs.parse(path)` or `keel.sources.extract(sid)` and diff with `keel.legal.compare(chunks_a, chunks_b)`.
2. Quarantine template/prior-matter placeholders and check defined terms, cross-references, dates, and liability/monetary caps.
3. Classify each material delta by risk (`Critical`, `Major`, `Moderate`, `Minor`), party impact, fallback position, and `UNKNOWN(reason)` ambiguities.
4. Validate structured review grid outputs with `keel.legal.validate_grid_cell(raw)` and run `keel.legal.lint(text)` on commentary.
5. Label output `DRAFT` or `READY FOR ATTORNEY REVIEW`; never send, execute, or mark final.
