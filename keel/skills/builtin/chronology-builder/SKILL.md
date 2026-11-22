---
name: chronology-builder
description: Build a source-bound event chronology with explicit date gaps and dispute flags.
modes: [legal, ga-ca]
---

## Chronology Builder
1. Extract source chunks via `keel.sources.extract(sid)` or `keel.docs.parse(path)` and generate candidate items with `keel.legal.chronology(chunks)`.
2. Bind every event to a stable source locator, exact date/time (or `UNKNOWN(undated)`), actors, and evidentiary posture (verified record vs. allegation).
3. Preserve conflicting dates, sequence gaps, and unreadable/truncated sources explicitly instead of interpolating.
4. Flag statutory or contractual deadline candidates for human verification without asserting unverified finality.
5. Store structured timeline state via `keel.state.put(data)` and label output `WORKPAPER` or `READY FOR ATTORNEY REVIEW`.
