#!/usr/bin/env python3
"""
benchmark_edge_llm.py
=====================
Measure real token throughput for small LLMs running under Ollama, using the
exact kind of prompt your LTTS hackathon demo will use (a J1939 anomaly ->
2-sentence mechanical summary).

Purpose: decide Option A (model runs ON the Pi) vs Option B (model runs on the
MacBook gateway). The deciding number is "time to produce a 2-sentence summary".
If that's comfortably under your threshold (default 10s) on the Pi, Option A is
viable. If not, fall back to Option B.

No third-party dependencies -- stdlib only, so you can run it on a fresh Pi with
nothing but Python 3 and Ollama installed.

USAGE
-----
  # On the Pi (Ollama running locally on :11434):
  python3 benchmark_edge_llm.py

  # Auto-pull any models you don't have yet:
  python3 benchmark_edge_llm.py --pull

  # Custom model list and more measured runs for stabler numbers:
  python3 benchmark_edge_llm.py --models qwen2.5:1.5b llama3.2:1b --runs 5

  # Point at the MacBook gateway to compare Option B numbers:
  python3 benchmark_edge_llm.py --host http://192.168.1.50:11434

  # Tighten/loosen the "is Option A viable?" cutoff:
  python3 benchmark_edge_llm.py --threshold 8
"""

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

# ----------------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------------

# Small models worth testing on a Pi 5. Ordered roughly smallest -> largest.
# 4-bit quantized variants are what Ollama pulls by default for these tags.
DEFAULT_MODELS = [
    "qwen2.5:0.5b",
    "qwen2.5:1.5b",
    "llama3.2:1b",
    "gemma2:2b",
    "llama3.2:3b",
]

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_RUNS = 3          # measured runs (after one warm-up)
DEFAULT_THRESHOLD = 10.0  # seconds; under this on the Pi => Option A viable
HTTP_TIMEOUT = 600        # generous: a cold 3B model on a Pi can be slow

# This mirrors the real demo: a flagged anomaly data frame in, a short
# mechanical summary out. Keep max_tokens modest -- you only want 2 sentences,
# and capping output keeps latency representative of the real loop.
SYSTEM_PROMPT = (
    "You are an on-vehicle maintenance assistant. Given a flagged sensor "
    "anomaly, reply with a 2-sentence mechanical summary: one sentence stating "
    "the likely fault, one stating the recommended action. Be terse and "
    "technical. No preamble."
)

USER_PROMPT = (
    "Anomaly flagged on asset DEERE-7R-014.\n"
    "Signal frame (J1939):\n"
    "  SPN 190 Engine Speed: 2100 rpm\n"
    "  SPN 110 Engine Coolant Temp: 104 C\n"
    "  SPN 100 Engine Oil Pressure: 38 kPa  <-- LOW for this rpm\n"
    "  SPN 175 Engine Oil Temp: 131 C\n"
    "Baseline oil pressure at 2100 rpm is ~340 kPa. Summarize."
)

# Cap generation so we measure a realistic 2-sentence response, not a ramble.
NUM_PREDICT = 80


# ----------------------------------------------------------------------------
# Ollama API helpers
# ----------------------------------------------------------------------------

def _post(host, path, payload):
    url = host.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(host, path):
    url = host.rstrip("/") + path
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_server(host):
    try:
        _get(host, "/api/tags")
        return True
    except Exception as e:  # noqa: BLE001 - we want any failure to surface clearly
        print(f"ERROR: cannot reach Ollama at {host} ({e})", file=sys.stderr)
        print("       Is Ollama running? Try:  ollama serve", file=sys.stderr)
        return False


def installed_models(host):
    try:
        tags = _get(host, "/api/tags")
        return {m["name"] for m in tags.get("models", [])}
    except Exception:  # noqa: BLE001
        return set()


def pull_model(host, model):
    """Stream a pull, printing progress lines so a slow Pi pull isn't silent."""
    print(f"  pulling {model} ...")
    url = host.rstrip("/") + "/api/pull"
    data = json.dumps({"name": model, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            last = ""
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                msg = json.loads(line)
                status = msg.get("status", "")
                if status and status != last:
                    print(f"    {status}")
                    last = status
        return True
    except Exception as e:  # noqa: BLE001
        print(f"    pull failed: {e}", file=sys.stderr)
        return False


def generate(host, model):
    """One non-streamed generation. Returns the raw response dict with timings."""
    payload = {
        "model": model,
        "system": SYSTEM_PROMPT,
        "prompt": USER_PROMPT,
        "stream": False,
        "options": {"num_predict": NUM_PREDICT, "temperature": 0.2},
    }
    return _post(host, "/api/generate", payload)


# ----------------------------------------------------------------------------
# Metric extraction
# ----------------------------------------------------------------------------

NS = 1_000_000_000  # nanoseconds per second (Ollama reports durations in ns)


def parse_metrics(resp):
    """Pull the numbers we care about out of an Ollama /api/generate response."""
    eval_count = resp.get("eval_count", 0)
    eval_dur = resp.get("eval_duration", 0) or 0
    prompt_count = resp.get("prompt_eval_count", 0)
    prompt_dur = resp.get("prompt_eval_duration", 0) or 0
    load_dur = resp.get("load_duration", 0) or 0
    total_dur = resp.get("total_duration", 0) or 0

    gen_tok_s = (eval_count / (eval_dur / NS)) if eval_dur else 0.0
    prompt_tok_s = (prompt_count / (prompt_dur / NS)) if prompt_dur else 0.0

    return {
        "gen_tokens": eval_count,
        "gen_tok_s": gen_tok_s,
        "prompt_tokens": prompt_count,
        "prompt_tok_s": prompt_tok_s,
        "load_s": load_dur / NS,
        "total_s": total_dur / NS,  # wall time to produce the summary
        "text": (resp.get("response") or "").strip(),
    }


def benchmark_model(host, model, runs):
    """Warm-up once (loads weights into RAM), then time `runs` measured calls."""
    print(f"\n=== {model} ===")
    try:
        print("  warm-up (loading model) ...", flush=True)
        warm = parse_metrics(generate(host, model))
        print(f"  warm-up load: {warm['load_s']:.1f}s  "
              f"(first response took {warm['total_s']:.1f}s total)")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        print(f"  SKIP: HTTP {e.code} -- {body[:160]}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"  SKIP: {e}", file=sys.stderr)
        return None

    totals, gen_rates = [], []
    sample_text = warm["text"]
    for i in range(runs):
        m = parse_metrics(generate(host, model))
        totals.append(m["total_s"])
        gen_rates.append(m["gen_tok_s"])
        sample_text = m["text"] or sample_text
        print(f"  run {i + 1}/{runs}: {m['total_s']:.2f}s total, "
              f"{m['gen_tok_s']:.1f} gen tok/s "
              f"({m['gen_tokens']} tokens)")

    return {
        "model": model,
        "median_total_s": statistics.median(totals),
        "median_gen_tok_s": statistics.median(gen_rates),
        "warm_load_s": warm["load_s"],
        "sample": sample_text,
    }


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------

def print_report(results, threshold):
    results = [r for r in results if r]
    if not results:
        print("\nNo models benchmarked successfully.", file=sys.stderr)
        return

    print("\n" + "=" * 68)
    print("SUMMARY  (warm runs; load time excluded from time-to-summary)")
    print("=" * 68)
    header = f"{'model':<18}{'time/summary':>14}{'gen tok/s':>12}{'verdict':>14}"
    print(header)
    print("-" * 68)
    for r in sorted(results, key=lambda x: x["median_total_s"]):
        viable = r["median_total_s"] <= threshold
        verdict = "A: on-Pi OK" if viable else "B: gateway"
        print(f"{r['model']:<18}{r['median_total_s']:>12.2f}s"
              f"{r['median_gen_tok_s']:>12.1f}{verdict:>14}")

    print("-" * 68)
    best = min(results, key=lambda x: x["median_total_s"])
    print(f"\nFastest: {best['model']} "
          f"({best['median_total_s']:.2f}s per 2-sentence summary).")
    if best["median_total_s"] <= threshold:
        print(f"--> Option A is VIABLE: at least one model produces a summary "
              f"under your {threshold:.0f}s cutoff on this device.")
        print("    Still cache your demo anomalies' outputs as a fallback.")
    else:
        print(f"--> Option A looks RISKY here: even the fastest model needs "
              f"{best['median_total_s']:.1f}s (> {threshold:.0f}s cutoff).")
        print("    Recommend Option B (MacBook gateway) + templated fallback.")

    print("\nSample output from fastest model:")
    print("  " + (best["sample"] or "(empty)").replace("\n", "\n  "))
    print("\nNote: 'time/summary' is wall time for a warm model (weights already "
          "in RAM).\nThe first request after boot also pays the load cost shown "
          "per model above;\npre-warm the model before your demo so you never pay "
          "it on stage.")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Benchmark small LLMs under Ollama for the LTTS edge-AI demo."
    )
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help=f"Ollama base URL (default {DEFAULT_HOST})")
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS,
                    help="Models to test (default: a small-model sweep)")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                    help=f"Measured runs per model (default {DEFAULT_RUNS})")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"Seconds; Option-A cutoff (default {DEFAULT_THRESHOLD})")
    ap.add_argument("--pull", action="store_true",
                    help="Auto-pull any listed models that aren't installed")
    args = ap.parse_args()

    if not check_server(args.host):
        sys.exit(1)

    have = installed_models(args.host)
    results = []
    for model in args.models:
        if model not in have:
            # Ollama tags can omit ':latest'; tolerate a loose match.
            loose = any(m.split(":")[0] == model.split(":")[0] and
                        m.split(":")[-1] == model.split(":")[-1] for m in have)
            if not loose:
                if args.pull:
                    if not pull_model(args.host, model):
                        print(f"  skipping {model} (pull failed)")
                        continue
                else:
                    print(f"\n=== {model} ===")
                    print(f"  SKIP: not installed. "
                          f"Run `ollama pull {model}` or pass --pull.")
                    continue
        results.append(benchmark_model(args.host, model, args.runs))

    print_report(results, args.threshold)


if __name__ == "__main__":
    main()