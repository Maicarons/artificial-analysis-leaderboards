#!/usr/bin/env python3
"""
Fetch Artificial Analysis leaderboard data from public web surfaces.
Saves daily snapshots + latest.json pointer.

Sources:
  - /leaderboards/models                                → LLM leaderboard page payload
  - /api/v2/data/media/<slug>  (key required)           → Live media boards when AA_API_KEY is set
  - /api/text-to-image/arena/preferences                → Text-to-image / image editing (legacy, gated)
  - /api/text-to-speech/arena/preferences               → Text-to-speech (legacy, gated)
  - /api/text-to-video/arena/preferences                → Text-to-video / image-to-video (legacy, gated)

When AA_API_KEY is provided (env or --api-key), the 5 media boards are fetched
live from the official v2 API. Otherwise the script falls back to the legacy
public endpoints, and — if those are gated — to the most recent historical
snapshot. The LLM board is always fetched from the page payload.

Usage:
  python3 scripts/fetch_leaderboards.py
  python3 scripts/fetch_leaderboards.py --api-key <AA_API_KEY>
  AA_API_KEY=... python3 scripts/fetch_leaderboards.py
  python3 scripts/fetch_leaderboards.py --only llms text-to-video
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
import urllib.error
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
PARSER_VERSION = "public-web-v1"

SOURCES = [
    {
        "slug": "llms",
        "source_type": "page_payload",
        "source_url": "https://artificialanalysis.ai/leaderboards/models",
        "description": "LLM leaderboard page payload",
    },
    {
        "slug": "text-to-image",
        "source_type": "public_json",
        "source_url": "https://artificialanalysis.ai/api/text-to-image/arena/preferences?supports_image_input=false",
        "description": "Text-to-image arena leaderboard",
    },
    {
        "slug": "image-editing",
        "source_type": "public_json",
        "source_url": "https://artificialanalysis.ai/api/text-to-image/arena/preferences?supports_image_input=true",
        "description": "Image editing leaderboard via text-to-image public arena endpoint",
    },
    {
        "slug": "text-to-speech",
        "source_type": "public_json",
        "source_url": "https://artificialanalysis.ai/api/text-to-speech/arena/preferences",
        "description": "Text-to-speech arena leaderboard",
    },
    {
        "slug": "text-to-video",
        "source_type": "public_json",
        "source_url": "https://artificialanalysis.ai/api/text-to-video/arena/preferences?supports-image-input=false",
        "description": "Text-to-video arena leaderboard",
    },
    {
        "slug": "image-to-video",
        "source_type": "public_json",
        "source_url": "https://artificialanalysis.ai/api/text-to-video/arena/preferences?supports-image-input=true",
        "description": "Image-to-video leaderboard via text-to-video public arena endpoint",
    },
]

# Endpoints whose public arena API now requires a logged-in user key.
# When fetching them fails (e.g. HTTP 400 "User key is required"), we fall
# back to the most recent historical snapshot that still had data instead of
# failing the whole run.
GATED_MEDIA_SLUGS: frozenset[str] = frozenset(
    {"text-to-image", "image-editing", "text-to-speech"}
)

GATED_NOTE: str = (
    "AA arena API now requires a logged-in user key; "
    "using last known public snapshot"
)

DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Official Artificial Analysis v2 API. When AA_API_KEY is available we fetch
# live media board data from here; the legacy public arena endpoints are now
# gated behind a login key. The key is ONLY ever read from the environment or
# the --api-key CLI flag — it is never written to disk.
AA_V2_BASE: str = "https://artificialanalysis.ai/api/v2/data"
AA_API_KEY: str = os.environ.get("AA_API_KEY", "")

# Every non-LLM (media) endpoint can be served by the v2 API.
MEDIA_SLUGS: frozenset[str] = frozenset(
    {s["slug"] for s in SOURCES if s["slug"] != "llms"}
)


def find_last_known_snapshot(
    repo_root: Path, slug: str, today: str
) -> tuple[Path | None, dict[str, Any] | None]:
    """Scan historical ``data/<date>/<slug>.json`` snapshots (newest first) and
    return the most recent one whose ``models`` array is non-empty.

    Args:
        repo_root: Repository root that contains the ``data`` directory.
        slug: Endpoint slug to look up.
        today: ISO date string (``YYYY-MM-DD``) to skip — only history is used.

    Returns:
        A tuple ``(path, content)`` for the most recent usable snapshot, or
        ``(None, None)`` when no suitable snapshot exists.
    """
    data_root = repo_root / "data"
    if not data_root.is_dir():
        return None, None

    date_names: list[str] = [
        entry.name
        for entry in data_root.iterdir()
        if entry.is_dir() and DATE_DIR_RE.match(entry.name) and entry.name != today
    ]
    date_names.sort(reverse=True)

    for date_name in date_names:
        candidate = data_root / date_name / f"{slug}.json"
        if not candidate.is_file():
            continue
        try:
            with open(candidate, encoding="utf-8") as f:
                content = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        models = content.get("models")
        if isinstance(models, list) and len(models) > 0:
            return candidate, content

    return None, None


def http_get(url: str, accept: str = "application/json") -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def fetch_json(url: str) -> dict[str, Any]:
    return json.loads(http_get(url).decode("utf-8"))


def fetch_text(url: str) -> str:
    return http_get(url, accept="text/html,*/*;q=0.8").decode("utf-8", "ignore")


def clean_value(value: Any) -> Any:
    if value == "$undefined":
        return None
    if isinstance(value, dict):
        return {k: clean_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_value(v) for v in value]
    return value


def format_ci95(ci_delta: Any) -> str | None:
    if ci_delta is None:
        return None
    if isinstance(ci_delta, float) and ci_delta.is_integer():
        ci_delta = int(ci_delta)
    return f"-{ci_delta}/+{ci_delta}"


def iter_nested(obj: Any) -> Iterable[Any]:
    yield obj
    if isinstance(obj, dict):
        for value in obj.values():
            yield from iter_nested(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from iter_nested(value)


def extract_llm_models_from_page(html: str) -> list[dict[str, Any]]:
    pattern = re.compile(r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)</script>')

    for match in pattern.finditer(html):
        decoded = json.loads(match.group(1))
        if '"models":' not in decoded or ':' not in decoded:
            continue

        payload = decoded.split(':', 1)[1]
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue

        for node in iter_nested(obj):
            if not isinstance(node, dict):
                continue
            models = node.get("models")
            if not isinstance(models, list) or not models:
                continue
            if isinstance(models[0], dict) and "modelCreatorId" in models[0]:
                return clean_value(models)

    raise RuntimeError("Could not locate detailed llm models payload in page HTML")


def pick_primary_elo(raw: dict[str, Any]) -> dict[str, Any]:
    overall = raw.get("overallElo")
    if isinstance(overall, dict):
        return overall

    for entry in raw.get("elos", []):
        if not entry.get("tag") and not entry.get("category") and not entry.get("accent"):
            return entry

    if raw.get("elos"):
        return raw["elos"][0]

    return {}


def normalize_elo_entry(entry: dict[str, Any]) -> dict[str, Any]:
    entry = clean_value(entry)
    return {
        "elo": entry.get("elo"),
        "appearances": entry.get("appearances"),
        "wins": entry.get("wins"),
        "win_rate": entry.get("winRate"),
        "ci95": format_ci95(entry.get("ciDelta")),
        "ci_delta": entry.get("ciDelta"),
        "tag": entry.get("tag"),
        "category": entry.get("category"),
        "accent": entry.get("accent"),
    }


def normalize_media(raw: dict[str, Any], slug: str, default_rank: int | None = None) -> dict[str, Any]:
    raw = clean_value(raw)
    primary = pick_primary_elo(raw)

    result = {
        "id": raw["id"],
        "name": raw["name"],
        "slug": raw["slug"],
        "release_date": raw.get("releaseDate"),
        "creator": raw.get("creator"),
        "family": raw.get("family"),
        "elo": primary.get("elo"),
        "rank": raw.get("overallRank", default_rank),
        "ci95": format_ci95(primary.get("ciDelta")),
        "appearances": primary.get("appearances"),
        "wins": primary.get("wins"),
        "win_rate": primary.get("winRate"),
        "open_weights_url": raw.get("openWeightsUrl"),
        "is_current": raw.get("isCurrent"),
        "is_scraped": raw.get("isScraped"),
        "introduced_at": raw.get("introducedAt"),
        "note": raw.get("note"),
        "elos": [normalize_elo_entry(entry) for entry in raw.get("elos", [])],
    }

    if "isFirstPartyFoundational" in raw:
        result["is_first_party_foundational"] = raw.get("isFirstPartyFoundational")

    if slug in {"text-to-image", "image-editing"}:
        result["pricing"] = {"price_per_1k_images": raw.get("pricePer1kImages")}
    elif slug == "text-to-speech":
        result["pricing"] = {"price_per_1m_characters": raw.get("pricePer1mCharacters")}
    else:
        result["pricing"] = {"price_per_minute": raw.get("pricePerMinute")}

    return result


def normalize_llm(raw: dict[str, Any]) -> dict[str, Any]:
    raw = clean_value(raw)
    return {
        "id": raw["id"],
        "name": raw["name"],
        "short_name": raw.get("shortName"),
        "slug": raw["slug"],
        "release_date": raw.get("releaseDate"),
        "reasoning_model": raw.get("reasoningModel"),
        "deprecated": raw.get("deprecated"),
        "creator": {
            "id": raw.get("modelCreatorId"),
            "name": raw.get("modelCreatorName"),
            "slug": raw.get("modelCreatorSlug"),
            "country": raw.get("modelCreatorCountry"),
            "color": raw.get("modelCreatorColor"),
            "logo": raw.get("modelCreatorLogo"),
        },
        "evaluations": {
            "artificial_analysis_intelligence_index": raw.get("intelligenceIndex"),
            "artificial_analysis_intelligence_index_is_estimated": raw.get("intelligenceIndexIsEstimated"),
            "artificial_analysis_coding_index": raw.get("codingIndex"),
            "artificial_analysis_agentic_index": raw.get("agenticIndex"),
            "tau2_bench": raw.get("tau2"),
            "terminal_bench_hard": raw.get("terminalbenchHard"),
            "scicode": raw.get("scicode"),
            "aa_lcr": raw.get("lcr"),
            "aa_omniscience": raw.get("omniscience"),
            "aa_omniscience_accuracy": raw.get("omniscienceAccuracy"),
            "aa_omniscience_non_hallucination": raw.get("omniscienceNonHallucination"),
            "ifbench": raw.get("ifbench"),
            "hle": raw.get("hle"),
            "gpqa": raw.get("gpqa"),
            "critpt": raw.get("critpt"),
            "apex_agents": raw.get("apexAgents"),
            "gdpval_aa_normalized": raw.get("gdpvalNormalized"),
            "mmmu_pro": raw.get("mmmuPro"),
        },
        "pricing": {
            "price_1m_blended_3_to_1": raw.get("price1mBlended3To1"),
            "price_1m_input_tokens": raw.get("price1mInputTokens"),
            "price_1m_output_tokens": raw.get("price1mOutputTokens"),
            "intelligence_index_cost_total": raw.get("intelligenceIndexCostTotal"),
            "intelligence_index_cost_input": raw.get("intelligenceIndexCostInput"),
            "intelligence_index_cost_output": raw.get("intelligenceIndexCostOutput"),
            "intelligence_index_cost_reasoning": raw.get("intelligenceIndexCostReasoning"),
            "intelligence_index_cost_answer": raw.get("intelligenceIndexCostAnswer"),
            "price_class": raw.get("priceClass"),
        },
        "speed": {
            "output_tokens_per_second": raw.get("medianOutputTokensPerSecond"),
            "time_to_first_token_seconds": raw.get("medianTimeToFirstTokenSeconds"),
            "time_to_first_answer_token_seconds": raw.get("medianTimeToFirstAnswerTokenSeconds"),
            "end_to_end_response_time_seconds": raw.get("medianEndToEndResponseTimeSeconds"),
            "reasoning_time_seconds": raw.get("medianReasoningTimeSeconds"),
            "percentile_05_output_tokens_per_second": raw.get("percentile05OutputTokensPerSecond"),
            "percentile_95_output_tokens_per_second": raw.get("percentile95OutputTokensPerSecond"),
            "quartile_25_output_tokens_per_second": raw.get("quartile25OutputTokensPerSecond"),
            "quartile_75_output_tokens_per_second": raw.get("quartile75OutputTokensPerSecond"),
            "percentile_05_time_to_first_token_seconds": raw.get("percentile05TimeToFirstTokenSeconds"),
            "percentile_95_time_to_first_token_seconds": raw.get("percentile95TimeToFirstTokenSeconds"),
            "quartile_25_time_to_first_token_seconds": raw.get("quartile25TimeToFirstTokenSeconds"),
            "quartile_75_time_to_first_token_seconds": raw.get("quartile75TimeToFirstTokenSeconds"),
        },
        "capabilities": {
            "context_window_tokens": raw.get("contextWindowTokens"),
            "total_parameters": raw.get("totalParameters"),
            "active_parameters": raw.get("activeParameters"),
            "training_tokens_trillions": raw.get("trainingTokensTrillions"),
            "size_class": raw.get("sizeClass"),
            "input_modality_text": raw.get("inputModalityText"),
            "input_modality_image": raw.get("inputModalityImage"),
            "input_modality_video": raw.get("inputModalityVideo"),
            "input_modality_speech": raw.get("inputModalitySpeech"),
            "output_modality_text": raw.get("outputModalityText"),
            "output_modality_image": raw.get("outputModalityImage"),
            "output_modality_video": raw.get("outputModalityVideo"),
            "output_modality_speech": raw.get("outputModalitySpeech"),
        },
        "open_weights": {
            "is_open_weights": raw.get("isOpenWeights"),
            "commercial_allowed": raw.get("commercialAllowed"),
            "license_name": raw.get("licenseName"),
            "license_url": raw.get("licenseUrl"),
            "huggingface_url": raw.get("huggingfaceUrl"),
            "openrouter_api_id": raw.get("openrouterApiId"),
        },
        "breakdowns": {
            "multilingual": raw.get("multilingualBreakdown"),
            "gdpval": raw.get("gdpvalBreakdown"),
            "omniscience": raw.get("omniscienceBreakdown"),
            "openness": raw.get("opennessBreakdown"),
            "eval_token_counts": raw.get("evalTokenCounts"),
            "intelligence_index_token_counts": raw.get("intelligenceIndexTokenCounts"),
        },
    }


def _parse_ci95_delta(ci95: Any) -> float | None:
    """Convert a v2 ci95 string such as ``"-7/7"`` into a numeric half-width delta.

    Returns ``None`` when the value is missing or cannot be parsed.
    """
    if not isinstance(ci95, str):
        return None
    parts = ci95.split("/")
    if len(parts) != 2:
        return None
    try:
        lo = float(parts[0])
        hi = float(parts[1])
    except ValueError:
        return None
    return (abs(lo) + abs(hi)) / 2.0


def _legacy_media_from_v2(item: dict[str, Any]) -> dict[str, Any]:
    """Map a single v2 media item onto the legacy media model shape.

    The mapping is crafted so the UNCHANGED ``normalize_media`` keeps the live
    ELO / rank / CI95 / appearances:
      - ``overallRank`` carries the v2 rank (normalize_media reads rank from it).
      - ``overallElo`` carries elo / appearances / ciDelta (normalize_media reads
        those via ``pick_primary_elo``).
    Top-level fields are retained for fidelity with the documented legacy shape;
    ``elos`` is intentionally left empty (per the legacy contract).
    """
    creator = item.get("model_creator") or {}
    elo = item.get("elo")
    rank = item.get("rank")
    appearances = item.get("appearances")
    ci95_raw = item.get("ci95")
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "slug": item.get("slug"),
        "release_date": item.get("release_date"),
        "creator": {
            "id": creator.get("id", ""),
            "name": creator.get("name", ""),
        },
        "elo": elo,
        "rank": rank,
        "overallRank": rank,
        "ci95": ci95_raw,
        "appearances": appearances,
        "family": None,
        "wins": None,
        "win_rate": None,
        "pricing": None,
        "elos": [],
        "overallElo": {
            "elo": elo,
            "appearances": appearances,
            "ciDelta": _parse_ci95_delta(ci95_raw),
            "wins": None,
            "winRate": None,
        },
        "open_weights_url": None,
        "is_current": None,
        "is_scraped": None,
        "introduced_at": None,
        "note": None,
    }


def fetch_media_v2(slug: str, api_key: str) -> list[dict[str, Any]]:
    """Fetch a media board from the official AA v2 API (requires ``x-api-key``).

    Returns a list of legacy-shaped media model dicts ready for ``normalize_media``.
    """
    url = f"{AA_V2_BASE}/media/{slug}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "x-api-key": api_key,
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    return [_legacy_media_from_v2(item) for item in data]


def fetch_source(source: dict[str, str], api_key: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    slug = source["slug"]
    source_url = source["source_url"]

    if slug == "llms":
        html = fetch_text(source_url)
        raw_models = extract_llm_models_from_page(html)
        models = [normalize_llm(model) for model in raw_models]
        source_type = source["source_type"]
        source_ref = source_url
        used_v2 = False
    else:
        used_v2 = bool(api_key) and slug in MEDIA_SLUGS
        try:
            if used_v2:
                raw_models = fetch_media_v2(slug, api_key)
            else:
                payload = fetch_json(source_url)
                raw_models = payload.get("models", [])
        except Exception:
            if used_v2:
                # v2 failed → gracefully fall back to the legacy public endpoint
                # (which may itself fail and then trigger the gated-snapshot
                # fallback in main()'s exception handler).
                payload = fetch_json(source_url)
                raw_models = payload.get("models", [])
                used_v2 = False
            else:
                raise
        models = [normalize_media(model, slug, default_rank=i) for i, model in enumerate(raw_models, start=1)]
        source_type = "aa_v2_api" if used_v2 else source["source_type"]
        source_ref = f"{AA_V2_BASE}/media/{slug}" if used_v2 else source_url

    meta = {
        "endpoint": slug,
        "source_type": source_type,
        "source_url": source_ref,
        "source_description": source["description"],
        "parser_version": PARSER_VERSION,
        "model_count": len(models),
    }
    if used_v2:
        meta["source"] = AA_V2_BASE
        meta["gated"] = False
    return models, meta


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Artificial Analysis leaderboards")
    parser.add_argument("--only", nargs="*", help="Only fetch these endpoint slugs")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between requests in seconds")
    parser.add_argument("--api-key", default=None, help="AA v2 API key (overrides the AA_API_KEY env var)")
    args = parser.parse_args()

    # Prefer an explicit --api-key flag, otherwise fall back to the env var.
    api_key = args.api_key or AA_API_KEY

    sources = SOURCES
    if args.only:
        wanted = set(args.only)
        sources = [source for source in SOURCES if source["slug"] in wanted]
        if not sources:
            print(f"ERROR: No matching endpoints for {args.only}", file=sys.stderr)
            sys.exit(1)

    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    fetched_at = now.isoformat()

    day_dir = repo_root / "data" / date_str
    day_dir.mkdir(parents=True, exist_ok=True)

    index_path = day_dir / "_index.json"
    if index_path.exists() and args.only:
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
        index["fetched_at"] = fetched_at
    else:
        index = {
            "date": date_str,
            "fetched_at": fetched_at,
            "source": "https://artificialanalysis.ai",
            "source_type": "public_web_surfaces",
            "parser_version": PARSER_VERSION,
            "endpoints": {},
        }

    success_count = 0
    gated_count = 0
    critical_failed = False
    total = len(sources)

    for i, source in enumerate(sources, start=1):
        slug = source["slug"]
        print(f"Fetching {slug}...", end=" ", flush=True)
        try:
            models, meta = fetch_source(source, api_key)
            meta["fetched_at"] = fetched_at
            output = {
                "meta": meta,
                "models": models,
            }

            out_path = day_dir / f"{slug}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(output, f, indent=2, ensure_ascii=False)

            index["endpoints"][slug] = {
                "model_count": len(models),
                "source_type": meta["source_type"],
                "source_url": meta["source_url"],
            }
            if "gated" in meta:
                index["endpoints"][slug]["gated"] = meta["gated"]
            success_count += 1
            print(f"✓ {len(models)} models")
        except Exception as e:
            if slug in GATED_MEDIA_SLUGS:
                snapshot_path, snapshot_content = find_last_known_snapshot(
                    repo_root, slug, date_str
                )
                if snapshot_path is not None and snapshot_content is not None:
                    original_date = snapshot_path.parent.name
                    meta = dict(snapshot_content.get("meta", {}))
                    meta["gated"] = True
                    meta["source"] = "last_known_snapshot"
                    meta["original_date"] = original_date
                    meta["note"] = GATED_NOTE
                    meta["fetched_at"] = fetched_at
                    snapshot_content["meta"] = meta

                    out_path = day_dir / f"{slug}.json"
                    with open(out_path, "w", encoding="utf-8") as f:
                        json.dump(snapshot_content, f, indent=2, ensure_ascii=False)

                    index["endpoints"][slug] = {
                        "model_count": len(snapshot_content.get("models", [])),
                        "source_type": source["source_type"],
                        "source_url": source["source_url"],
                        "gated": True,
                        "note": GATED_NOTE,
                    }
                    success_count += 1
                    gated_count += 1
                    print(
                        f"↑ {len(snapshot_content.get('models', []))} models "
                        f"(gated → last snapshot {original_date})"
                    )
                    if i < total:
                        time.sleep(args.delay)
                    continue
            # Non-gated failure, or gated slug with no usable fallback.
            print(f"✗ {e}", file=sys.stderr)
            index["endpoints"][slug] = {
                "error": str(e),
                "source_type": source["source_type"],
                "source_url": source["source_url"],
            }
            if slug == "llms":
                critical_failed = True

        if i < total:
            time.sleep(args.delay)

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    latest_path = repo_root / "data" / "latest.json"
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "path": f"data/{date_str}"}, f, indent=2)

    print(
        f"\nDone: {success_count}/{total} endpoints, "
        f"{gated_count} gated (fallback), saved to data/{date_str}/"
    )

    # Only a failure of the critical `llms` endpoint is fatal. Media endpoints
    # that are gated behind a login key degrade gracefully (fall back to a
    # historical snapshot) and must not produce a non-zero exit code.
    if critical_failed:
        print("CRITICAL: `llms` fetch failed — exiting non-zero", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
