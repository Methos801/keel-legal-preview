---
name: authority-audit
description: Verify citations, quotations, scope, and currentness of legal and factual propositions.
modes: [legal, ga-ca]
---

## Authority Audit
1. Identify every material legal and factual proposition, including confident uncited assertions.
2. Verify each citation, quotation, and record fact against primary/source text via `keel.sources.extract(sid)` or `keel.docs.parse(path)`; never reconstruct quotes from memory.
3. Classify support scope: `SUPPORTED`, `LIMITED`, `OVERBROAD`, `POSTURE_MISMATCH`, `JURISDICTION_MISMATCH`, `UNSUPPORTED`, or `UNVERIFIED`.
4. Check currentness (amendment, supersession, overruling, effective date); if citator access is unavailable, mark `UNKNOWN(currentness unconfirmed)`.
5. Run `keel.legal.lint(text)` and report severity (`Critical`, `Major`, `Moderate`, `Minor`) plus verdict (`PASS`, `PASS-WITH-NOTES`, `REPAIR-REQUIRED`, `FAIL`). `PASS` does not authorize external action.
