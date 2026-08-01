#!/usr/bin/env python3
"""
Fetch Artificial Analysis leaderboard data from public web surfaces.
Saves daily snapshots + latest.json pointer.

Sources:
  - /leaderboards/models                                → LLM leaderboard page payload
  - /api/text-to-image/arena/preferences                → Text-to-image / image editing
  - /api/text-to-speech/arena/preferences               → Text-to-speech
  - /api/text-to-video/arena/preferences                → Text-to-video / image-to-video

Usage:
  python3 scripts/fetch_leaderboards.py
  python3 scripts/fetch_leaderboards.py --only llms text-to-video
"""

from __future__ import annotations

import argparse
import json
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


def fetch_source(source: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    slug = source["slug"]
    source_url = source["source_url"]

    if slug == "llms":
        html = fetch_text(source_url)
        raw_models = extract_llm_models_from_page(html)
        models = [normalize_llm(model) for model in raw_models]
    else:
        payload = fetch_json(source_url)
        raw_models = payload.get("models", [])
        models = [normalize_media(model, slug, default_rank=i) for i, model in enumerate(raw_models, start=1)]

    meta = {
        "endpoint": slug,
        "source_type": source["source_type"],
        "source_url": source_url,
        "source_description": source["description"],
        "parser_version": PARSER_VERSION,
        "model_count": len(models),
    }
    return models, meta


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Artificial Analysis leaderboards")
    parser.add_argument("--only", nargs="*", help="Only fetch these endpoint slugs")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between requests in seconds")
    args = parser.parse_args()

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
            models, meta = fetch_source(source)
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
                "source_type": source["source_type"],
                "source_url": source["source_url"],
            }
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
