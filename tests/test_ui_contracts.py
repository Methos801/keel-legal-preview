"""Automated UI contract, XSS hardening, and Backend/CLI parity tests."""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path




INDEX_HTML = Path(__file__).resolve().parents[1] / "keel" / "ui" / "web" / "static" / "index.html"

VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class _StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.unbalanced: list[str] = []
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for k, v in attrs:
            if k == "id" and v is not None:
                self.ids.append(v)
        if tag not in VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for k, v in attrs:
            if k == "id" and v is not None:
                self.ids.append(v)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            return
        if not self.stack:
            self.unbalanced.append(f"unexpected </{tag}>")
            return
        if self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.unbalanced.append(f"expected </{self.stack[-1]}>, got </{tag}>")


class UIContractsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_html_tags_balanced_and_ids_unique(self) -> None:
        parser = _StructureParser()
        parser.feed(self.html)
        parser.close()
        self.assertEqual(parser.unbalanced, [])
        self.assertEqual(parser.stack, [])

        seen: set[str] = set()
        dups: list[str] = []
        for elem_id in parser.ids:
            if elem_id in seen:
                dups.append(elem_id)
            seen.add(elem_id)
        self.assertEqual(dups, [], f"Duplicate HTML IDs found: {dups}")

    def test_inline_script_passes_node_syntax_check(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node binary not installed")
        scripts = re.findall(r"<script[^>]*>([\s\S]*?)</script>", self.html)
        self.assertEqual(len(scripts), 1)
        with tempfile.TemporaryDirectory() as td:
            js_path = Path(td) / "studio_inline.js"
            js_path.write_text(scripts[0], encoding="utf-8")
            proc = subprocess.run([node, "--check", str(js_path)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"node --check failed: {proc.stderr}")

    def test_hold_gate_and_legal_lint_detail_formatting_behavior(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node binary not installed")
        scripts = re.findall(r"<script[^>]*>([\s\S]*?)</script>", self.html)
        self.assertEqual(len(scripts), 1)
        script_src = re.sub(r"\brefreshAll\(\);\s*$", "", scripts[0].strip())

        driver = """
const assert = require('assert');
const vm = require('vm');

function makeEl(id) {
  return {
    id,
    innerHTML: '',
    textContent: '',
    value: '',
    style: {},
    dataset: {},
    disabled: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
  };
}

const elements = new Map();
function getEl(id) {
  if (!elements.has(id)) elements.set(id, makeEl(id));
  return elements.get(id);
}

const ctx = {
  console,
  URLSearchParams,
  setTimeout: () => 0,
  clearTimeout: () => {},
  AbortController: class { abort() {} },
  document: {
    getElementById: (id) => getEl(id),
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (tag) => makeEl(tag),
    addEventListener: () => {},
  },
  fetch: async (url) => {
    if (url === '/api/legal/lint') {
      return {
        ok: true,
        text: async () => JSON.stringify({
          ok: false,
          label: 'HOLD',
          findings: [
            { severity: 'error', rule: 'unresolved_citation', detail: 'Citation [S9999] was not found in the source ledger.' },
          ],
          citations: ['S0001', 'S9999'],
          source_ids: [{id:'S0001',found:true}, {id:'S9999',found:false}],
        }),
      };
    }
    return { ok: true, text: async () => '{}' };
  },
};
vm.createContext(ctx);
vm.runInContext(
  script_src + "\\nglobalThis.__testHooks = { renderLabState, runLegalLint };",
  ctx
);

(async () => {
  ctx.__testHooks.renderLabState({
    decision: 'HOLD',
    sources_count: 1,
    manifest_sha256: 'abc123',
    banks: [],
    anomalies: [],
    holds: [
      { source_id: 'S0001', rel_path: 'ledger_sample.csv', code: 'ambiguous_table', severity: 'HOLD', detail: 'row 2 column count (1) != header (3)' },
    ],
  });
  const holdsHtml = getEl('lab-holds-list').innerHTML;
  assert(holdsHtml.includes('HOLD Gate · S0001 · ledger_sample.csv'), 'HOLD header missing: ' + holdsHtml);
  assert(holdsHtml.includes('[ambiguous_table] row 2 column count (1) != header (3)'), 'HOLD detail missing: ' + holdsHtml);
  assert(!holdsHtml.includes('{"source_id"'), 'HOLD card fell back to raw JSON: ' + holdsHtml);

  getEl('legal-text').value = 'Claim [S0001]';
  await ctx.__testHooks.runLegalLint();
  const legalHtml = getEl('legal-structured').innerHTML;
  const cardPart = legalHtml.split('<details')[0];
  assert(cardPart.includes('[unresolved_citation] Citation [S9999] was not found in the source ledger.'), 'Lint detail missing: ' + cardPart);
  assert(cardPart.includes('S0001</strong>: Found in the sample ledger'));
  assert(cardPart.includes('S9999</strong>: Not found in the sample ledger'));
  assert(!/Citations verified|Compliant Workpaper/i.test(cardPart));
  assert(!cardPart.includes('{"severity"'), 'Lint finding fell back to raw JSON: ' + cardPart);
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""
        full_js = f"const script_src = {json.dumps(script_src)};\n{driver}"
        with tempfile.TemporaryDirectory() as td:
            driver_path = Path(td) / "test_detail_formatting.js"
            driver_path.write_text(full_js, encoding="utf-8")
            proc = subprocess.run([node, str(driver_path)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"Detail formatting JS behavior failed:\n{proc.stderr}\n{proc.stdout}")

