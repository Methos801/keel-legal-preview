# Keel legal preview

Trace a date in a draft back to its record. This small Python example builds a chronology from a sample delivery note and checks whether a source ID appears in a ledger. The full Keel Studio project interface is not part of this download.

[Illustrated walkthrough](https://methos801.github.io/keel-legal-preview/)

## Try it on Windows

Download and extract the source ZIP. Open PowerShell in the folder containing `pyproject.toml`. With Python 3.12 installed:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install .
.\.venv\Scripts\python -B -m keel.cli chronology
.\.venv\Scripts\python -B -m keel.inspector
```

The chronology pairs April 2, 2026 with `S0001`, page 1. Open the local address printed by the inspector. Check `[S0001 p. 1]`, then `[S9999 p. 1]`: only the first ID appears in the sample ledger. A match does not verify the page, quotation or conclusion. Stop the inspector with Ctrl+C.

Setup may fetch dependencies. The examples do not need a model server, API key or private documents. Run `python -B -m keel.cli skills` with the environment’s Python to list the four built-in legal skills.

## Development

With the same Python, run `python -B -m unittest discover -s tests -v`. Two UI tests also use Node.js. The provider test uses an in-memory HTTP transport.

[License](LICENSE). PyYAML (MIT) and HTTPX (BSD-3-Clause) are separate dependencies.
