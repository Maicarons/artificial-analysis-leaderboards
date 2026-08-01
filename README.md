# Artificial Analysis Leaderboards

[![Daily Fetch](https://github.com/oolong-tea-2026/artificial-analysis-leaderboards/actions/workflows/fetch.yml/badge.svg)](https://github.com/oolong-tea-2026/artificial-analysis-leaderboards/actions/workflows/fetch.yml)

Daily snapshots of [Artificial Analysis](https://artificialanalysis.ai) leaderboard data collected from public web surfaces.

## Data

| Endpoint | Description | Data |
|----------|-------------|------|
| `llms` | LLM leaderboard | Intelligence, coding, agentic, pricing, speed, context window, modality support |
| `text-to-image` | Text-to-image arena rankings | ELO, rank, CI95, appearances, win rate, per-tag breakdowns |
| `image-editing` | Image editing arena rankings | ELO, rank, CI95, appearances, win rate |
| `text-to-speech` | Text-to-speech arena rankings | ELO, rank, CI95, appearances, win rate, category/accent breakdowns |
| `text-to-video` | Text-to-video arena rankings | ELO, rank, CI95, appearances, win rate, per-tag breakdowns |
| `image-to-video` | Image-to-video arena rankings | ELO, rank, CI95, appearances, win rate, per-tag breakdowns |

## Sources

This repo intentionally avoids relying on the rate-limited free AA API for daily full snapshots.

Instead it collects from Artificial Analysis public web surfaces:

- `llms` → `https://artificialanalysis.ai/leaderboards/models`
- `text-to-image` → `https://artificialanalysis.ai/api/text-to-image/arena/preferences?supports_image_input=false`
- `image-editing` → `https://artificialanalysis.ai/api/text-to-image/arena/preferences?supports_image_input=true`
- `text-to-speech` → `https://artificialanalysis.ai/api/text-to-speech/arena/preferences`
- `text-to-video` → `https://artificialanalysis.ai/api/text-to-video/arena/preferences?supports-image-input=false`
- `image-to-video` → `https://artificialanalysis.ai/api/text-to-video/arena/preferences?supports-image-input=true`

## Structure

```text
data/
├── latest.json          # → {"date": "2026-04-10", "path": "data/2026-04-10"}
├── 2026-04-10/
│   ├── _index.json      # Daily summary
│   ├── llms.json
│   ├── text-to-image.json
│   ├── image-editing.json
│   ├── text-to-speech.json
│   ├── text-to-video.json
│   └── image-to-video.json
```

Each data file includes:

- `meta.endpoint`
- `meta.source_type`
- `meta.source_url`
- `meta.parser_version`
- `meta.fetched_at`
- `meta.model_count`

## Quick Access

```bash
# Latest pointer
curl -s https://raw.githubusercontent.com/oolong-tea-2026/artificial-analysis-leaderboards/main/data/latest.json

# Latest LLM snapshot
curl -s https://raw.githubusercontent.com/oolong-tea-2026/artificial-analysis-leaderboards/main/data/2026-04-10/llms.json
```

## Updates

Data is fetched daily at 05:13 UTC via GitHub Actions.

## Attribution

Data provided by [Artificial Analysis](https://artificialanalysis.ai). Please provide attribution when reusing the data. See their [methodology](https://artificialanalysis.ai/methodology) for benchmark details.

## License

MIT — Data attribution to Artificial Analysis required.

## 已知说明 (Known Issues)

The media arena endpoints **Text-to-Image / Image Editing / Text-to-Speech** now require a logged-in user key: Artificial Analysis changed the public arena API (`/api/<media>/arena/preferences`) so that it returns `HTTP 400 {"error":"User key is required"}` for unauthenticated requests. There is no public alternative endpoint (the leaderboard pages are client-rendered with no embedded data).

When the daily fetch hits this, `scripts/fetch_leaderboards.py` **degrades gracefully** instead of failing:
- It falls back to the most recent historical `data/<date>/<slug>.json` snapshot that still contains model data.
- The promoted `data/<today>/<slug>.json` keeps the original payload but its `meta` is annotated with `gated: true`, `source: "last_known_snapshot"`, `original_date`, and a `note` explaining the gated API.
- The daily `_index.json` records `gated: true` for those endpoints rather than only an `error`.

The **LLM leaderboard (`llms`) is unaffected** and is refreshed normally. Text-to-Video / Image-to-Video remain empty boards (0 models) by design. The script only exits non-zero when the critical `llms` endpoint itself fails.
