#!/usr/bin/env python3
"""One-shot snapshot of provider request counts for a single LLM Gateway model.

Designed to run from GitHub Actions on a schedule: it appends one line to
history.jsonl, fills logos.json on first run, and compacts old points so the
file the browser downloads stays small forever.

Usage:  python3 collect.py [--model deepseek-v4-flash]
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

BASE = "https://internal.llmgateway.io"
SITE = "https://llmgateway.io"
HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(HERE, "history.jsonl")
LOGOS_PATH = os.path.join(HERE, "logos.json")

# resolution kept per age: (older than N hours -> at most one point per M minutes)
COMPACTION = [(24 * 30, 6 * 60), (24 * 2, 60)]


def _fetch(url, as_json=True):
    req = urllib.request.Request(
        url, headers={"Accept": "*/*", "User-Agent": "Mozilla/5.0 (llmgateway-trends)"}
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if as_json else body


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ts(snap):
    return datetime.fromisoformat(snap["ts"])


# ---------------------------------------------------------------- provider logos

def _extract_logo(html, provider_name):
    m = re.search(r'<svg[^>]*class="[^"]*h-24 w-24[^"]*".*?</svg>', html, re.S)
    if not m and provider_name:
        m = re.search(r'<svg[^>]*aria-label="%s".*?</svg>' % re.escape(provider_name), html, re.S)
    if not m:
        cands = [x.group(0) for x in re.finditer(r"<svg\b.*?</svg>", html, re.S)
                 if "lucide" not in x.group(0)]
        return max(cands, key=len) if cands else None
    return m.group(0)


def _sanitize_svg(svg, pid):
    svg = re.sub(r"<script\b.*?</script>", "", svg, flags=re.S | re.I)
    svg = re.sub(r'\son\w+\s*=\s*"[^"]*"', "", svg, flags=re.I)
    svg = re.sub(r'\sclass="[^"]*"', "", svg)
    for gid in set(re.findall(r'id="([^"]+)"', svg)):
        svg = svg.replace(f'id="{gid}"', f'id="{pid}-{gid}"')
        svg = svg.replace(f"url(#{gid})", f"url(#{pid}-{gid})")
        svg = svg.replace(f'href="#{gid}"', f'href="#{pid}-{gid}"')
    return re.sub(r"<svg\b", '<svg width="100%" height="100%"', svg, count=1)


def _sync_logos(names):
    logos = {}
    if os.path.exists(LOGOS_PATH):
        with open(LOGOS_PATH, encoding="utf-8") as f:
            logos = json.load(f)
    missing = [p for p in names if p not in logos]
    for pid in missing:
        try:
            svg = _extract_logo(_fetch(f"{SITE}/providers/{pid}", as_json=False), names[pid])
            if svg:
                logos[pid] = _sanitize_svg(svg, pid)
        except Exception as exc:
            print(f"logo {pid}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if missing:
        with open(LOGOS_PATH, "w", encoding="utf-8") as f:
            json.dump(logos, f, ensure_ascii=False)
        print(f"logos: +{len(missing)} attempted, {len(logos)} stored")


# ---------------------------------------------------------------- history

def _snapshot(model):
    provs = _fetch(f"{BASE}/internal/models/{model}/benchmarks").get("providers", [])
    return {
        "ts": _utc(),
        "windowHours": provs[0]["windowHours"] if provs else None,
        "providers": [{
            "id": p["providerId"],
            "name": p.get("providerName") or p["providerId"],
            "requests": p.get("logsCount") or 0,
            "errors": p.get("errorsCount") or 0,
            "cached": p.get("cachedCount") or 0,
            "errorRate": p.get("errorRate"),
            "uptime": p.get("uptime"),
            "tps": p.get("tokensPerSecond"),
            "ttft": p.get("avgTimeToFirstToken"),
        } for p in provs],
    }


def _load_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    out = []
    with open(HISTORY_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                snap = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "providers" in snap and "ts" in snap:
                out.append(snap)
    return out


def _compact(history, now):
    """Thin out old points so the browser never downloads a huge file.

    The newest point of every bucket wins, and the very last snapshot is always
    kept so the dashboard shows current numbers.
    """
    kept, last_kept_at = [], {}
    for snap in history:
        age_h = (now - _ts(snap)).total_seconds() / 3600
        step = next((m for older, m in COMPACTION if age_h > older), None)
        if step is None:
            kept.append(snap)
            continue
        bucket = int(_ts(snap).timestamp() // (step * 60))
        if last_kept_at.get(step) != bucket:
            last_kept_at[step] = bucket
            kept.append(snap)
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek-v4-flash")
    args = ap.parse_args()

    snap = _snapshot(args.model)
    if not snap["providers"]:
        print("upstream returned no providers, nothing written", file=sys.stderr)
        return 1

    _sync_logos({p["id"]: p["name"] for p in snap["providers"]})

    history = _load_history()
    prev = history[-1] if history else None
    same = prev and {p["id"]: p["requests"] for p in prev["providers"]} == \
                    {p["id"]: p["requests"] for p in snap["providers"]}
    if same:
        history[-1] = snap          # numbers unchanged, just refresh recency
    else:
        history.append(snap)

    now = datetime.now(timezone.utc)
    before = len(history)
    history = _compact(history, now)

    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        for s in history:
            f.write(json.dumps(s, separators=(",", ":")) + "\n")

    total = sum(p["requests"] for p in snap["providers"])
    print(f"{snap['ts']}  {len(snap['providers'])} providers  {total} requests  "
          f"points {before}->{len(history)}  {'(unchanged)' if same else '(new)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
