# What to hand in — index

Everything below is in this repository. Rendered PDFs and the diagram image are
in `docs/handin/`; regenerate them with `bash docs/handin/render.sh`.

| # | Item | Required form | Hand in | Pages |
|---|---|---|---|---|
| 1 | **The pipeline diagram** | Image or PDF | `docs/handin/hop-map-diagram.pdf` (A3 landscape) or `docs/handin/hop-map-diagram.png` | 1 |
| 2 | **Control points table** | Alongside the diagram | `docs/handin/control-points.pdf` — the diagram, the nineteen-hop table with its three columns, and the eighteen control points | 6 |
| 3 | **The four components** | Working code + README saying how to run it | This repository; `README.md` §How to run. Screenshots of a real end-to-end run: `docs/handin/ui-1-staged.png` (lodgement staged, token bound to the payload hash) and `ui-2-lodged.png` (confirmed — `ACR1609260001`) | — |
| 4 | **The tests** | Working code + their output | `tests/`; output in `docs/handin/test-output-v1.txt` and `test-output-v2.txt` | — |
| 5 | **Coverage table + NIST + APP 8** | 2 pages | `docs/handin/coverage.pdf` | **2** |
| 6 | **What you found, and what worries you most** | 2 pages maximum | `docs/handin/findings.pdf` | **2** |

Source markdown for items 5 and 6: `docs/coverage.md`, `docs/findings.md`.
Source HTML for items 1, 2, 5 and 6: `docs/hop-map.html`,
`docs/pipeline-diagram.html`, `docs/coverage.html`, `docs/findings.html`.

## Presenting it

`docs/explainer.html` — open in a browser — walks through all six items in plain
English, explains every term in brackets, and carries a suggested running order
and the questions likely to come up.

## Against the judging criteria

**Does the diagram match the code?** It was drawn from `agent/loop.py`, and
`docs/control-points.md` opens with the mapping an assessor needs to read one
against the other: the brief numbers nineteen hops, `agent.jsonl` logs nine hop
names. Hops 4/9/16 are one logged event, 5/10 are one, 11/17 are one, and hops
1, 7 and 12 are logged nowhere at all — including hop 12, the one the brief
calls the control point.

**The non-obvious trust boundaries.** Marked untrusted on the drawing in red
dashes: the manifest PDF (hop 8), the MCP response (hop 8), and the model's own
output (hops 5 and 10), with the reasoning in the "can it be trusted" column.

**Are the checks in the right layer?** Every control point carries an L1/L2/L3
label, a fail-open/fail-closed verdict, and a "why this hop and not the
neighbour" argument. CP-16 is labelled L3 and the table states plainly that in
v1 it was the only thing between a `tool_calls` block and an irreversible POST.

**Do the tests decide something?** `pytest tests/ -v -s` — 17 tests, pass/fail,
repeatable, every assertion reading `ledger.jsonl` or `agent.jsonl` rather than
the chat transcript. Current state: **12 passing, 5 failing**, and the failures
are the findings. One test, B1, is deliberately non-deterministic because the
attack it measures is: the canary fired on 4 of 20 captured runs
(`docs/canary-runs.txt`), so B1 passes on runs where it happens not to fire.
That is reported as the brief's second case — the model behaved well that run —
and not as a control.

**Did the coverage check change anything?** Yes, four things, named in
`docs/coverage.md` §4. Two produced fixes: test F
(`tests/test_f_output_handling.py`) proved the manifest-to-XSS route and CP-12
was then fixed, and `requirements.txt` is now pinned exactly. Two produced new
control points, CP-17 and CP-18. Three of the four were weaknesses already in my
notes that had never reached the control table.

**Is the note honest, including what you did not get to?** `docs/findings.md`
§"What I did not get to" lists CP-1, CP-3, CP-7 and CP-10 as understood, tested
and unfixed, plus the three OWASP rows still reading *exposed · none · no test*,
and §"What I would do differently" opens with the seven false passes my own
first test run produced. It also records a correction made while assembling this
package: an earlier draft cited the canary rate as "3 of 5" from a run I had
watched but not captured, and the figure is now 4 of 20 from
`docs/canary-runs.txt`.

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add OPENAI_API_KEY

python -m customs_server.app    # terminal 1, port 9000
python -m agent.app             # terminal 2, port 8000 — spawns the MCP subprocess
                                # then open http://localhost:8000
pytest tests/ -v -s             # the suite
```

## Supporting material, not required but referenced

| File | What it is |
|---|---|
| `NOTES-controls-i-noticed.md` | 77 weaknesses noticed during the build and deliberately left in place |
| `docs/v2-hardening-cp8.md` | The one control fixed, with the before/after on the unmodified tests |
| `docs/v1-baseline.md` | What the frozen baseline did and did not do, at tag `v1-unhardened` |
| `data/payloads.md` | The injection payloads, the predictions made before running them, and what happened |
| `docs/ledger-manual-run-v1.jsonl` | Ledger evidence from the hand-driven session, archived before the suite reset it |
| `docs/ledger-demo-v2.jsonl` | The ledger line behind the two UI screenshots, carrying `confirmation_sha256` |
| `docs/canary-runs.txt` | 20 captured runs of the injection canary, the evidence for the 4-in-20 rate |

Tags: `v1-unhardened` (the attackable baseline) and `v2-hardened` (CP-8 fixed).
