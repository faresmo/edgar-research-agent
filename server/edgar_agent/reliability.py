"""Build evals/reliability.json: measured reliability by question type and model, from the saved eval runs.

    python -m edgar_agent.reliability [--runs runs] [--plugin-results plugin/evals/results] [--eval-file evals/fab2_public.csv] [--out evals/reliability.json]

Two sources. The stand-alone agent's eval runs (`runs/eval-*`) and the plugin eval reports written by
`claude plugin eval` (`plugin/evals/results/*/aggregate-result.json`, the plugin as installed in Claude).
When plugin reports exist they are what the header quotes, since that is what a user actually gets; the
stand-alone figures are kept under "standalone" for the spec and the README.

The plugin's header sentence, its connection instructions and the README table read this file, so the
numbers a user sees are always the measured ones. Only runs of the named eval file count (the public
Finance Agent v2.1 set by default), only questions that were answered or judged (not API errors), and a
run is attributed to the model family recorded in its settings (haiku / sonnet / opus / fable / local).
Where a model ran a question several times, every run counts: the score is the mean over runs, so the
figure already carries the run-to-run variance rather than a lucky single run.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List


def _family(model: str) -> str:
    m = (model or "").lower()
    if m == "local" or "qwen" in m or "mlx" in m:
        return "local"
    for fam in ("haiku", "sonnet", "opus", "fable"):
        if fam in m:
            return fam
    return ""


def _run_model(run_dir: str) -> str:
    for f in glob.glob(os.path.join(run_dir, "q*", "settings.json")):
        try:
            s = json.load(open(f))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(s, dict):
            vals = s.get("values", s)
            m = vals.get("model") or ""
            if m == "local":
                return vals.get("local_model") or "local"
            return m
    return ""


def _run_eval_file(run_dir: str) -> str:
    for f in glob.glob(os.path.join(run_dir, "q*", "settings.json")):
        try:
            s = json.load(open(f))
        except (OSError, json.JSONDecodeError):
            continue
        vals = s.get("values", s) if isinstance(s, dict) else {}
        if vals.get("eval_file"):
            return str(vals["eval_file"])
    return ""


def _questions_in(eval_file: str) -> set:
    """The question texts of the eval set, so only runs of that set are counted (settings do not record the file)."""
    import csv
    out = set()
    try:
        with open(eval_file, newline="") as fh:
            for row in csv.DictReader(fh):
                q = row.get("Question") or row.get("question") or ""
                if q.strip():
                    out.add(" ".join(q.split()))
    except OSError:
        pass
    return out


def build(runs_root: str, eval_file: str) -> Dict:
    want = os.path.basename(eval_file)
    wanted_questions = _questions_in(eval_file)
    per_type: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    per_type_raw: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    overall: Dict[str, List[float]] = defaultdict(list)
    overall_raw: Dict[str, List[float]] = defaultdict(list)
    costs: Dict[str, List[float]] = defaultdict(list)
    runs_used: Dict[str, int] = defaultdict(int)
    for run_dir in sorted(glob.glob(os.path.join(runs_root, "eval-*"))):
        fam = _family(_run_model(run_dir))
        if not fam:
            continue
        ef = _run_eval_file(run_dir)
        if ef and os.path.basename(ef) != want:
            continue
        evals = glob.glob(os.path.join(run_dir, "q*", "eval.json"))
        if not evals:
            continue
        # Only full runs of the set count: a probe of the seven hardest questions says nothing about the model's
        # reliability on the set, and would drag its average down (Opus 14% from one such probe, 1 Nov).
        loaded = []
        for f in evals:
            try:
                e = json.load(open(f))
            except (OSError, json.JSONDecodeError):
                continue
            if str(e.get("stopped_because", "")).startswith("error") or not e.get("judge_ok", True):
                continue
            if wanted_questions and " ".join(str(e.get("question", "")).split()) not in wanted_questions:
                continue
            loaded.append(e)
        if wanted_questions and len(loaded) < 0.8 * len(wanted_questions):
            continue  # a run cut short (billing, crash) is not a measurement of the set
        counted = 0
        for e in loaded:
            total = e.get("criteria_total") or 0
            if not total:
                continue
            official = e["criteria_met"] / total
            raw = sum(n.get("points", 1) for n in e.get("judge_notes", []) if n.get("met") and not str(n.get("criterion", "")).startswith("- ")) / total
            qt = e.get("question_type") or "unknown"
            per_type[qt][fam].append(official); per_type_raw[qt][fam].append(min(raw, 1.0))
            overall[fam].append(official); overall_raw[fam].append(min(raw, 1.0))
            costs[fam].append(float(e.get("cost_usd") or 0.0))
            counted += 1
        if counted:
            runs_used[fam] += 1
    out = {"generated": time.strftime("%Y-%m-%d"), "eval_file": want, "note": "official = fully-correct criteria share under the set's must-pass gate; "
           "raw = rubric points earned; means over every run of every question, so run-to-run variance is included.",
           "runs_per_model": dict(runs_used), "types": {}, "overall": {}}
    for qt, fams in sorted(per_type.items()):
        out["types"][qt] = {fam: {"official": round(sum(v) / len(v), 3), "raw": round(sum(per_type_raw[qt][fam]) / len(v), 3), "n": len(v)} for fam, v in fams.items()}
    for fam, v in overall.items():
        out["overall"][fam] = {"official": round(sum(v) / len(v), 3), "raw": round(sum(overall_raw[fam]) / len(v), 3), "n": len(v),
                               "cost_per_question": round(sum(costs[fam]) / len(costs[fam]), 3) if costs[fam] else 0.0}
    return out


_PLUGIN_HEADER = re.compile(r"answered by Claude (\w+)", re.I)


def _fab2_rubrics(eval_file: str) -> List[list]:
    import csv
    out = []
    try:
        with open(eval_file, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    out.append(json.loads(row.get("Rubric") or "[]"))
                except json.JSONDecodeError:
                    out.append([])
    except OSError:
        pass
    return out


def _fab2_types(eval_file: str) -> List[str]:
    import csv
    try:
        with open(eval_file, newline="", encoding="utf-8") as fh:
            return [row.get("Question Type", "") for row in csv.DictReader(fh)]
    except OSError:
        return []


def build_from_plugin(results_root: str, eval_file: str, since: str = "") -> Dict:
    """The same table from `claude plugin eval` reports. A case is `fab2-NN-...`; its graders `cNN` are the
    rubric lines in order (evals/make_plugin_evals.py), so the must-pass gate is applied here from the
    rubric. The model is read from the header sentence Claude replied with ("answered by Claude Opus");
    a case without one takes the run's majority model. Partial runs (fewer than 80% of the set) are skipped."""
    rubrics, types = _fab2_rubrics(eval_file), _fab2_types(eval_file)
    per_type: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    per_type_raw: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    overall: Dict[str, List[float]] = defaultdict(list)
    overall_raw: Dict[str, List[float]] = defaultdict(list)
    costs: Dict[str, List[float]] = defaultdict(list)
    runs_used: Dict[str, int] = defaultdict(int)
    for f in sorted(glob.glob(os.path.join(results_root, "*", "aggregate-result.json"))):
        if since and os.path.basename(os.path.dirname(f)) < since:
            continue  # 6 Nov: measure the current plugin only, not runs on earlier versions
        try:
            d = json.load(open(f))
        except (OSError, json.JSONDecodeError):
            continue
        cases = [c for c in d.get("cases", []) if str(c.get("name", "")).startswith("fab2-")]
        if rubrics and len(cases) < 0.8 * len(rubrics):
            continue
        scored = []
        for c, arm in [(c, arm) for c in cases for arm in ((c.get("arms") or {}).get("with") or [])]:  # every run (--runs 3), not only the first
            if not arm or arm.get("error"):
                continue
            try:
                i = int(c["name"][5:7]) - 1
            except ValueError:
                continue
            rubric = rubrics[i] if i < len(rubrics) else []
            crit = [g for g in arm.get("graders", []) if re.fullmatch(r"c\d+", g.get("name", ""))]
            if not crit:
                continue
            pts = sum(g.get("weight", 1) for g in crit)
            got = sum(g.get("weight", 1) for g in crit if g.get("passed"))
            gated = any(not g.get("passed") and int(g["name"][1:]) - 1 < len(rubric) and rubric[int(g["name"][1:]) - 1].get("must_pass") for g in crit)
            fam = ""
            for g in arm.get("graders", []):
                m = _PLUGIN_HEADER.search(str(g.get("evidence") or ""))
                if m:
                    fam = _family(m.group(1))
                    break
            qt = types[i] if i < len(types) else "unknown"
            scored.append((qt, fam, 0.0 if gated else got / pts, got / pts, float(arm.get("costUsd") or 0.0)))
        if not scored:
            continue
        fams = [s[1] for s in scored if s[1]]
        majority = max(set(fams), key=fams.count) if fams else ""
        if not majority:
            continue
        for qt, fam, official, raw, cost in scored:
            fam = fam or majority
            per_type[qt][fam].append(official); per_type_raw[qt][fam].append(raw)
            overall[fam].append(official); overall_raw[fam].append(raw); costs[fam].append(cost)
        runs_used[majority] += 1
    # runs per model = answers per question (a 3-run folder counts three times), not result folders
    out = {"runs_per_model": {fam: round(len(v) / max(1, len(rubrics))) for fam, v in overall.items()}, "types": {}, "overall": {}}
    for qt, fams in sorted(per_type.items()):
        out["types"][qt] = {fam: {"official": round(sum(v) / len(v), 3), "raw": round(sum(per_type_raw[qt][fam]) / len(v), 3), "n": len(v)} for fam, v in fams.items()}
    for fam, v in overall.items():
        out["overall"][fam] = {"official": round(sum(v) / len(v), 3), "raw": round(sum(overall_raw[fam]) / len(v), 3), "n": len(v),
                               "cost_per_question": round(sum(costs[fam]) / len(costs[fam]), 3) if costs[fam] else 0.0}
    return out


def render_table(table: Dict) -> str:
    """Markdown for the README and the first reply: models across, overall first, then by type."""
    fams = sorted(table.get("overall", {}), key=lambda f: table["overall"][f]["official"])
    if not fams:
        return ""
    lines = ["| Question type | " + " | ".join(f.capitalize() for f in fams) + " |", "|---|" + "---|" * len(fams)]
    lines.append("| All questions | " + " | ".join(f"{int(round(table['overall'][f]['official'] * 100))}%" for f in fams) + " |")
    for qt, row in sorted(table.get("types", {}).items()):
        lines.append(f"| {qt} | " + " | ".join((f"{int(round(row[f]['official'] * 100))}%" if f in row else "–") for f in fams) + " |")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="runs")
    ap.add_argument("--eval-file", default="evals/fab2_public.csv")
    ap.add_argument("--plugin-results", default="plugin/evals/results")
    ap.add_argument("--out", default="evals/reliability.json")
    ap.add_argument("--plugin-since", default="", help="only plugin result folders named on or after this (e.g. 2026-11-06T16-58)")
    args = ap.parse_args(argv)
    standalone = build(args.runs, args.eval_file)
    plugin = build_from_plugin(args.plugin_results, args.eval_file, args.plugin_since)
    if plugin["overall"]:
        table = {"generated": standalone["generated"], "eval_file": standalone["eval_file"],
                 "note": "Measured through the plugin as installed in Claude (claude plugin eval): official = fully-correct share under the "
                         "set's must-pass gate; raw = rubric points earned; means over every run of every question. 'standalone' holds the "
                         "same measures for the stand-alone agent (API)." + (f" Plugin results from {args.plugin_since} on (the current plugin; "
                         "graders credit two values only when both are legitimate and explained, so not comparable with the Vals leaderboard)." if args.plugin_since else ""),
                 "source": "plugin", **{k: plugin[k] for k in ("runs_per_model", "types", "overall")},
                 "standalone": {k: standalone[k] for k in ("runs_per_model", "types", "overall")}}
    else:
        table = standalone
    Path(args.out).write_text(json.dumps(table, indent=1))
    print(f"wrote {args.out} from {table.get('source', 'standalone')} runs: {table['runs_per_model']}; models {sorted(table['overall'])}")
    print(render_table(table))
    if table.get("standalone"):
        print("\nstand-alone agent:", table["standalone"]["runs_per_model"])
        print(render_table(table["standalone"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
