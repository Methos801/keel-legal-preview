"""Deterministic legal-workflow controls (§19.4).

Pure-code deterministic controls adopted from Local Harness:
- Document output linting: finality claim detection, unsourced deadlines, unverified filings,
  ledger citation validation, and status labels.
- Chronology extraction from chunked sources.
- Chunk-level document comparison.
- Review grid deterministic extraction and validation.

These are code, NOT prompts. They do not give legal advice or compute deadlines.
"""

from __future__ import annotations

import csv
import datetime as dt
import difflib
import io
import json
from pathlib import Path
import re
from typing import Any, Callable

FINALITY = re.compile(
    r"\b(final(?!\s+(?:order|judgment|hearing|pretrial|pre-trial|notice|demand|payoff|determination|decision|ruling|disposition|appeal))"
    r"|filing[- ]ready|court[- ]ready|client[- ]ready|service[- ]ready|legally verified|fully verified|approved for (?:filing|release|service)|certified)\b",
    re.I,
)
NEGATED = re.compile(r"\b(not|never|no|until|before|without|cannot be|isn't|is not|nor)\b[^.\n]{0,40}$", re.I)
DEADLINE = re.compile(
    r"\b(the (?:answer |response |filing )?deadline is|is due (?:on|by)|must be (?:filed|served|answered) (?:on|by)|expires? on|due date is|last day to)\b[^.\n]{0,60}",
    re.I,
)
FILED_CLAIM = re.compile(r"\b(was|were|has been|have been|is|are)\s+(?:duly\s+)?(filed|served|recorded|docketed|entered)\b", re.I)
CITE = re.compile(r"\[(S\d{4}(?:\.a\d+)*)(?:[^\]]*)\]")

DATE_PATTERNS = [
    (
        re.compile(
            r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
            re.I,
        ),
        "mdy_text",
    ),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b"), "mdy_num"),
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
    (
        re.compile(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s+day of\s+(January|February|March|April|May|June|July|August|September|October|November|December),?\s+(\d{4})\b",
            re.I,
        ),
        "dom_text",
    ),
]

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _parse_date(kind: str, g: tuple[str, ...]) -> dt.date | None:
    try:
        if kind == "mdy_text":
            return dt.date(int(g[2]), MONTHS[g[0][:3].lower()], int(g[1]))
        if kind == "mdy_num":
            y = int(g[2])
            y = y + 2000 if y < 100 else y
            return dt.date(y, int(g[0]), int(g[1]))
        if kind == "iso":
            return dt.date(int(g[0]), int(g[1]), int(g[2]))
        if kind == "dom_text":
            return dt.date(int(g[2]), MONTHS[g[1][:3].lower()], int(g[0]))
    except Exception:
        return None
    return None


def _sentences(text: str):
    for m in re.finditer(r"[^.\n]+(?:\.(?!\s|$)[^.\n]*)*[.\n]?", text):
        s = m.group(0)
        if s.strip():
            yield m.start(), s


def lint_legal_output(
    text: str,
    resolve_sid: Callable[[str], bool] | None = None,
    allowed_labels: list[str] | None = None,
    require_citations: bool = True,
) -> dict[str, Any]:
    """Deterministic validation of legal work product.

    Returns:
        {
            "ok": bool,
            "label": str | None,
            "findings": [{"severity": "error"|"warn", "rule": str, "detail": str}],
            "citations": [str, ...],
        }
    """
    findings: list[dict[str, str]] = []
    labels = allowed_labels or [
        "DRAFT",
        "WORKPAPER",
        "READY FOR ATTORNEY REVIEW",
        "READY FOR INTERNAL REVIEW",
        "HOLD",
    ]

    # 1. Label check on first lines
    head = "\n".join(text.strip().splitlines()[:8]).upper()
    label = next((l for l in sorted(labels, key=len, reverse=True) if l in head), None)
    if not label:
        findings.append({
            "severity": "error",
            "rule": "label_missing",
            "detail": f"No permitted status label in header. Use one of: {', '.join(labels)}.",
        })

    # 2. Finality claim detection
    for m in FINALITY.finditer(text):
        pre = text[max(0, m.start() - 60):m.start()]
        if NEGATED.search(pre):
            continue
        snippet = text[max(0, m.start() - 30):min(len(text), m.end() + 30)].strip()
        findings.append({
            "severity": "error",
            "rule": "finality_claim",
            "detail": f"Forbidden finality claim '{m.group(0)}' near: …{snippet}…",
        })

    # 3. Sentence-level unsourced deadlines and claims
    for _, s in _sentences(text):
        if DEADLINE.search(s) and not CITE.search(s) and not re.search(
            r"(source|record|materials?|docket)[^.]{0,30}(report|show|indicate|state)|verify|possible|appears?|reported",
            s,
            re.I,
        ):
            findings.append({
                "severity": "error",
                "rule": "unsourced_deadline",
                "detail": f"Deadline stated as fact without citation or source qualifiers: '{s.strip()[:140]}'",
            })
        if FILED_CLAIM.search(s) and not CITE.search(s):
            findings.append({
                "severity": "warn",
                "rule": "unsupported_filing_claim",
                "detail": f"'filed/served/recorded' claim without a source citation: '{s.strip()[:140]}'",
            })

    # 4. Citation resolution check
    cites = sorted(set(re.findall(r"\[(S\d{4}(?:\.a\d+)*)", text)))
    if resolve_sid is not None:
        for sid in cites:
            if not resolve_sid(sid):
                findings.append({
                    "severity": "error",
                    "rule": "unresolved_citation",
                    "detail": f"Citation [{sid}] was not found in the source ledger.",
                })

    # 5. Bullet without citation check
    if require_citations:
        for ln in text.splitlines():
            t = ln.strip()
            if (re.match(r"^[-*•]\s+", t) and re.search(r"\d|\b[A-Z][a-z]+ [A-Z][a-z]+\b", t)
                    and not CITE.search(t)
                    and not re.search(r"\((inferred|inference|assumption|gap|verify|open|recommendation)", t, re.I)
                    and len(t) > 40):
                findings.append({
                    "severity": "warn",
                    "rule": "uncited_bullet",
                    "detail": f"Factual-looking bullet without [S####] citation or (inferred)/(gap) tag: '{t[:120]}'",
                })

    return {
        "ok": not any(f["severity"] == "error" for f in findings),
        "label": label,
        "findings": findings,
        "citations": cites,
    }


def extract_chronology(chunks: list[dict[str, Any]], max_items: int = 400) -> list[dict[str, Any]]:
    """Deterministically extracts chronological candidate items from document chunks."""
    candidates = []
    for c in chunks:
        text = c.get("text", "")
        sid = c.get("sid", "S0000")
        locator = c.get("locator", "")
        for rx, kind in DATE_PATTERNS:
            for m in rx.finditer(text):
                d = _parse_date(kind, m.groups())
                if not d or not (1990 <= d.year <= 2100):
                    continue
                start_pos = max(0, m.start() - 80)
                end_pos = min(len(text), m.end() + 100)
                snippet = text[start_pos:end_pos].replace("\n", " ").strip()
                candidates.append({
                    "date": d.isoformat(),
                    "sid": sid,
                    "locator": locator,
                    "context": snippet,
                    "cite": f"[{sid} {locator}]".strip(),
                })

    seen = set()
    deduped = []
    for item in sorted(candidates, key=lambda x: (x["date"], x["sid"], x["locator"])):
        key = (item["date"], item["sid"], item["context"][:60])
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    return deduped[:max_items]


def format_chronology_md(items: list[dict[str, Any]]) -> str:
    """Format chronology items as workpaper markdown."""
    lines = [
        "WORKPAPER - Chronology CANDIDATES",
        "(Date mentions discovered in extracted text; each requires source verification; not legal deadlines)",
        "",
    ]
    for o in items:
        lines.append(f"- **{o['date']}** - …{o['context']}… {o['cite']}")
    return "\n".join(lines)


def compare_chunks(
    chunks_a: list[dict[str, Any]],
    chunks_b: list[dict[str, Any]],
    max_hunks: int = 80,
) -> dict[str, Any]:
    """Sequence comparison between document versions."""
    a = [(c.get("locator", ""), re.sub(r"\s+", " ", c.get("text", "")).strip()) for c in chunks_a]
    b = [(c.get("locator", ""), re.sub(r"\s+", " ", c.get("text", "")).strip()) for c in chunks_b]
    sm = difflib.SequenceMatcher(None, [x[1] for x in a], [x[1] for x in b], autojunk=False)
    hunks = []
    stats = {"equal": 0, "replace": 0, "delete": 0, "insert": 0}

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        stats[tag] += max(i2 - i1, j2 - j1)
        if tag == "equal":
            continue
        hunks.append({
            "type": tag,
            "a_loc": [x[0] for x in a[i1:i2]][:6],
            "b_loc": [x[0] for x in b[j1:j2]][:6],
            "a_text": " ".join(x[1] for x in a[i1:i2])[:700],
            "b_text": " ".join(x[1] for x in b[j1:j2])[:700],
        })

    return {
        "similarity": round(sm.ratio(), 4),
        "stats": stats,
        "hunks": hunks[:max_hunks],
        "truncated": len(hunks) > max_hunks,
    }


def format_compare_md(label_a: str, label_b: str, res: dict[str, Any]) -> str:
    """Format chunk comparison result to markdown."""
    lines = [
        f"WORKPAPER - Comparison {label_a} -> {label_b}",
        f"Similarity: {res['similarity']} | Changed blocks: {sum(v for k, v in res['stats'].items() if k != 'equal')}",
        "",
    ]
    for h in res["hunks"]:
        lines.append(
            f"- **{h['type'].upper()}** A {h['a_loc']} / B {h['b_loc']}\n"
            f"  - A: {h['a_text'] or '—'}\n"
            f"  - B: {h['b_text'] or '—'}"
        )
    if res["truncated"]:
        lines.append("\n(hunk list truncated)")
    return "\n".join(lines)


def validate_review_grid_cell(raw: str) -> dict[str, Any]:
    """Parse and validate JSON review grid cell output."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"answer": "", "locators": [], "found": False, "confidence": "low", "error": "No JSON object found"}
    try:
        data = json.loads(m.group(0))
        if not isinstance(data, dict):
            return {"answer": "", "locators": [], "found": False, "confidence": "low", "error": "Not a dict"}
        return {
            "answer": str(data.get("answer", "")).strip()[:500],
            "locators": [str(loc) for loc in data.get("locators", [])],
            "found": bool(data.get("found", False)),
            "confidence": str(data.get("confidence", "low")).lower(),
        }
    except Exception as exc:
        return {"answer": "", "locators": [], "found": False, "confidence": "low", "error": str(exc)}
