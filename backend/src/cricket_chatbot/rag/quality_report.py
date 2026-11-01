"""Turn a quality report (backend/var/eval/quality_*.json) into a self-contained HTML page.

Usage: uv run python -m cricket_chatbot.rag.quality_report [report.json]   (default: the latest report)
"""

import html
import json
import math
import sys
from pathlib import Path

from ..config import BACKEND_DIR

EVAL_DIR = BACKEND_DIR / "var" / "eval"
e = lambda v: html.escape(str(v))


def _fmt(v, pct=False):
    if v is None:
        return "–"
    return f"{v * 100:.0f}%" if pct else f"{v:g}" if isinstance(v, (int, float)) else e(v)


def _tile(label, value, note="", tone=""):
    return f'<div class="tile {tone}"><div class="v">{value}</div><div class="l">{e(label)}</div><div class="n">{e(note)}</div></div>'


def _calibration(c: dict | None) -> str:
    if not c:
        return '<section><h2>1 · Judge calibration</h2><p class="muted">The judge was not run (<code>--no-judge</code>): only exact number checks were used.</p></section>'
    u, la = c.get("untouched_half", {}), c.get("llm_alone", {})
    badge = '<span class="badge ok">TRUSTED</span>' if c["trusted"] else '<span class="badge bad">NOT TRUSTED · advisory only</span>'
    warn = "" if c.get("labels_reviewed_by_human") else (
        '<p class="warn">The calibration labels were drafted by Claude and have not been reviewed by a human yet. '
        'Check <code>knowledge/judge_calibration.json</code> and set <code>"reviewed": true</code> before relying on the judge.</p>')
    rows = "".join(f"<tr><td>{d['id']}</td><td>{e(d['claim'])}</td><td>{e(d['human'])}</td><td>{e(d['judge'])}</td>"
                   f"<td class='muted'>{e(d['judge_reason'])}</td></tr>" for d in c.get("disagreements", []))
    return f"""<section><h2>1 · Judge calibration {badge}</h2>
<p class="muted">Before trusting the grader, it is scored against human labels on {c['items']} claims. Grader = exact number match, then the LLM judge.
Gate: ≥ 85% agreement and kappa ≥ 0.70, on all claims and on the half never inspected while tuning the judge prompt.</p>
<div class="tiles">{_tile("agreement", _fmt(c['accuracy'], True), f"kappa {c['kappa']}", "ok" if c['accuracy'] >= .85 else "bad")}
{_tile("untouched half", _fmt(u.get('accuracy'), True), f"kappa {u.get('kappa')} · {u.get('items')} claims", "ok" if u.get('trusted') else "bad")}
{_tile("catches bad claims", _fmt(c['catches_unsupported'], True), "unsupported claims flagged")}
{_tile("false alarms", _fmt(c['false_alarms'], True), "good claims flagged", "ok" if (c['false_alarms'] or 0) <= .1 else "bad")}
{_tile("LLM judge alone", _fmt(la.get('accuracy'), True), f"kappa {la.get('kappa')}")}</div>{warn}
<details><summary>Disagreements with the human labels ({len(c.get('disagreements', []))})</summary>
<table><tr><th>#</th><th>Claim</th><th>Human</th><th>Grader</th><th>Judge's check</th></tr>{rows}</table></details></section>"""


def _latency(s: dict) -> str:
    lat = s["latency_ms"]
    top = math.log10(max((v["p95"] or 0) for v in lat.values()) + 1) or 1
    scale = lambda ms: math.log10((ms or 0) + 1) / top * 100  # log scale: 30 s of LLM would hide a 3 ms stage
    rows = ""
    for st, v in lat.items():
        w50, w95 = scale(v["p50"]), scale(v["p95"])
        rows += (f"<tr><td>{e(st)}</td><td class='num'>{_fmt(v['p50'])}</td><td class='num'>{_fmt(v['p95'])}</td>"
                 f"<td class='barcell'><div class='bar p95' style='width:{w95:.1f}%'></div><div class='bar p50' style='width:{w50:.1f}%'></div></td></tr>")
    paths = "".join(f"<tr><td>{e(p)}</td><td class='num'>{v['n']}</td><td class='num'>{_fmt(v['p50'])}</td><td class='num'>{_fmt(v['p95'])}</td></tr>"
                    for p, v in s["latency_by_path_ms"].items())
    return f"""<section><h2>5 · Latency per stage</h2><p class="muted">From the OpenTelemetry spans of each answer, in milliseconds. Bars are p95 (light) and p50 (dark), on a log scale.</p>
<div class="two"><table><tr><th>Stage</th><th class="num">p50</th><th class="num">p95</th><th></th></tr>{rows}</table>
<table><tr><th>Answer path</th><th class="num">n</th><th class="num">p50</th><th class="num">p95</th></tr>{paths}</table></div></section>"""


def _case(i: int, c: dict) -> str:
    tone = {"ok": "ok", "retrieval_miss": "bad", "generation_error": "warnc"}[c["verdict"]]
    label = {"ok": "ok", "retrieval_miss": "retrieval miss", "generation_error": "generation error"}[c["verdict"]]
    if c["verdict"] == "ok" and any(x.get("judge") == "unsupported" for x in c["claims"]):
        tone, label = "warnc", "ok · judge flags (advisory)"  # an untrusted judge can't fail an answer, but say it objected
    claims = "".join(
        f"<li class='{'bad' if x['bad_numbers'] or x.get('judge') == 'unsupported' else 'muted' if x.get('judge') == 'error' else 'ok'}'>{e(x['claim'])}"
        f"<span class='muted'> · numbers not in evidence: {e(', '.join(x['bad_numbers']) or 'none')}"
        + (f" · judge: {e(x['judge'])} – {e(x.get('judge_reason', ''))}" if 'judge' in x else "") + "</span></li>"
        for x in c["claims"]) or "<li class='muted'>No checkable claims (refusal, scope reply or no evidence).</li>"
    probs = "".join(f"<li class='bad'>{e(p)}</li>" for p in c["answer_problems"]) or "<li class='ok'>All required facts present</li>"
    return f"""<details class="case"><summary><span class="badge {tone}">{label}</span><span class="q">{i}. {e(c['q'])}</span>
<span class="meta">{e(c['path'])} · {c['latency']['total']:.0f} ms</span></summary>
<div class="body"><p><b>Answer:</b> {e(c['answer'])}</p>
<p><b>Retrieval first:</b> needed source reached the agent: <b>{'yes' if c['retrieval_ok'] else 'NO'}</b> · sources used: {e(', '.join(c['used_sources']) or 'none')}<br>
recall@1 {_fmt(c['recall@1'])} · recall@3 {_fmt(c['recall@3'])} · recall@5 {_fmt(c['recall@5'])} · context precision {_fmt(c['context_precision'])} · top 5: {e(', '.join(c['top5']) or '–')}</p>
<p><b>Facts:</b></p><ul>{probs}</ul><p><b>Claims:</b></p><ul>{claims}</ul>
<p class="muted">Stages (ms): {' · '.join(f'{k} {v:g}' for k, v in c['latency'].items())}{f" · first word {c['first_token_ms']} ms" if c.get('first_token_ms') else ''}</p></div></details>"""


def render(rep: dict, name: str = "") -> str:
    s = rep.get("summary") or {}
    r, a, g, t = s.get("retrieval", {}), s.get("answers", {}), s.get("groundedness", {}), s.get("triage", {})
    body = _calibration(rep.get("calibration"))
    if s:
        body += f"""<section><h2>2 · Retrieval</h2><p class="muted">Hybrid search (top 5) scored against labelled chunks, on questions not used for tuning.
Context precision = share of the chunks the agent actually used that are labelled relevant (a lower bound).</p>
<div class="tiles">{_tile("recall@1", _fmt(r['recall@1'], True))}{_tile("recall@3", _fmt(r['recall@3'], True))}{_tile("recall@5", _fmt(r['recall@5'], True))}
{_tile("MRR", _fmt(r['mrr']))}{_tile("context precision", _fmt(r['context_precision'], True))}
{_tile("needed source reached agent", _fmt(r['needed_source_reached_agent'], True), "", "ok" if r['needed_source_reached_agent'] == 1 else "bad")}</div></section>
<section><h2>3 · Answers and groundedness</h2><div class="tiles">
{_tile("correct facts", f"{a['correct']}/{a['of']}", "required facts present, wrong ones absent", "ok" if a['correct'] == a['of'] else "bad")}
{_tile("claims checked", g['claims'])}
{_tile("unsupported numbers", g['claims_with_unsupported_numbers'], "exact match against evidence", "ok" if not g['claims_with_unsupported_numbers'] else "bad")}
{_tile("judge: unsupported", _fmt(g['judge_unsupported']), g['judge_counts'], "" if g['judge_unsupported'] is None else "ok" if not g['judge_unsupported'] else "warnc")}
{_tile("judge errors", _fmt(g.get('judge_errors')), "claims the judge couldn't grade (not counted)", "" if not g.get('judge_errors') else "warnc")}
{_tile("mean groundedness", _fmt(g['mean_grounded'], True))}</div></section>
<section><h2>4 · Triage: retrieval checked first</h2><p class="muted">A bad answer is a <b>retrieval miss</b> if the source it needed never reached the agent; otherwise it's a <b>generation error</b>.</p>
<div class="tiles">{_tile("ok", t['ok'], "", "ok")}{_tile("retrieval miss", t['retrieval_miss'], "", "bad" if t['retrieval_miss'] else "")}
{_tile("generation error", t['generation_error'], "", "warnc" if t['generation_error'] else "")}</div></section>
{_latency(s)}
<section><h2>6 · Every question</h2>{''.join(_case(i, c) for i, c in enumerate(rep['cases'], 1))}</section>"""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RAG Evaluation Report</title><style>
:root{{--bg:#f6f8f6;--card:#fff;--ink:#10241a;--muted:#5d6d63;--line:#dfe6e1;--ok:#2f6b3f;--okbg:#e6f1e9;--bad:#b0222f;--badbg:#f8e5e7;--warn:#9a6a10;--warnbg:#fbf1dc;--bar:#2f6b3f;--bar2:#b9d4c0}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0e1712;--card:#15211a;--ink:#e6eee9;--muted:#9aaba1;--line:#26352c;--ok:#7cc48f;--okbg:#18301f;--bad:#ef7b86;--badbg:#3a1a1e;--warn:#e7b65a;--warnbg:#33290f;--bar:#7cc48f;--bar2:#2b4a34}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:24px 16px 60px}}h1{{font-size:26px;margin:0 0 4px}}h2{{font-size:18px;margin:0 0 6px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
section{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:16px 0}}
.muted{{color:var(--muted)}}.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:10px}}
.tile{{border:1px solid var(--line);border-radius:10px;padding:12px}}.tile .v{{font-size:26px;font-weight:700}}.tile .l{{font-weight:600}}.tile .n{{color:var(--muted);font-size:13px}}
.tile.ok{{background:var(--okbg)}}.tile.ok .v{{color:var(--ok)}}.tile.bad{{background:var(--badbg)}}.tile.bad .v{{color:var(--bad)}}.tile.warnc{{background:var(--warnbg)}}.tile.warnc .v{{color:var(--warn)}}
.badge{{font-size:12px;font-weight:700;padding:2px 8px;border-radius:99px;white-space:nowrap}}.badge.ok{{background:var(--okbg);color:var(--ok)}}.badge.bad{{background:var(--badbg);color:var(--bad)}}.badge.warnc{{background:var(--warnbg);color:var(--warn)}}
.warn{{background:var(--warnbg);color:var(--warn);padding:10px 12px;border-radius:8px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}}.num{{text-align:right;font-variant-numeric:tabular-nums}}
.two{{display:grid;grid-template-columns:3fr 2fr;gap:16px}}@media (max-width:760px){{.two{{grid-template-columns:1fr}}}}
.barcell{{width:45%;position:relative}}.bar{{height:9px;border-radius:5px;position:absolute;left:8px;top:12px;max-width:calc(100% - 16px)}}.bar.p95{{background:var(--bar2)}}.bar.p50{{background:var(--bar)}}
details{{margin-top:8px}}summary{{cursor:pointer}}.case{{border:1px solid var(--line);border-radius:10px;padding:10px 12px}}
.case summary{{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}}.case .q{{font-weight:600;flex:1;min-width:200px}}.case .meta{{color:var(--muted);font-size:13px}}
.case .body{{padding-top:8px;overflow-wrap:anywhere}}li.bad{{color:var(--bad)}}li.ok{{color:var(--ok)}}code{{font-size:13px}}
.wrap{{overflow-x:auto}}</style></head><body><main>
<h1>RAG + LLM evaluation</h1><p class="muted">{e(name)} · run {e(rep.get('started', ''))} · judge model {e(rep.get('judge_model') or 'none')} · {s.get('questions', 0)} questions</p>
{body}</main></body></html>"""


def write_html(json_path: Path) -> Path:
    out = json_path.with_suffix(".html")
    out.write_text(render(json.loads(json_path.read_text()), json_path.name))
    return out


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else max(EVAL_DIR.glob("quality_*.json"), key=lambda p: p.stat().st_mtime)
    print(write_html(src))


if __name__ == "__main__":
    main()
