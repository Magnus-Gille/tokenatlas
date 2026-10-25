# Berget per-request emissions

TokenAtlas accepts Berget's published `usage.emissions` shape as a **provider-reported model estimate** for an individual request. The CO₂e value combines operational and embodied emissions; `energy_kwh` is IT energy. Training, network traffic and end-user devices are outside this boundary. The values are separate from TokenAtlas's token-based energy proxy and are never substituted for it.

Berget's [release note](https://berget.ai/en/blog/per-request-co2-press-release), [proposed response type](https://github.com/berget-ai/co2-calculator/blob/main/packages/co2-calculator/src/api-emissions.ts), and [methodology](https://github.com/berget-ai/co2-calculator/blob/main/METHODOLOGY.md) define the supported fields. On 2026-10-24, a live `POST /v1/chat/completions` request using `mistralai/Mistral-Small-3.2-24B-Instruct-2506` returned `usage.emissions` in the ordinary response and in the final usage event of a streamed response (`stream_options.include_usage=true`). Both objects passed TokenAtlas's validator. The public [OpenAPI document](https://api.berget.ai/openapi.json) still did not declare this response field at that time, so compatibility with every model and endpoint is not established.

## Local capture and offline import

When Pi retains `message.usage.emissions` for a Berget response, `tokenatlas refresh --harness pi` captures it with the same request ID as the token observation. The normal history snapshot/import path preserves it across machines. No prompt or response body enters the emissions field.

For a response captured separately, refresh the harness history first, then use:

```sh
tokenatlas import-emissions ./berget-emissions.jsonl
tokenatlas report --provider berget --records
```

Each JSONL line identifies an existing Berget observation and provides the published object:

```json
{"harness":"pi","request_id":"response-id","model":"openai/gpt-oss-120b","timestamp":"2026-10-21T12:00:00Z","usage":{"emissions":{"co2e_grams":0.04,"energy_kwh":0.0002,"operational":{"co2e_grams":0.03,"energy_kwh":0.0002},"embodied":{"co2e_grams":0.01},"grid":{"region":"SE3","carbon_intensity_gco2e_per_kwh":150},"methodology":"berget-co2-calculator","methodology_version":"1.0.0"}}}
```

`harness` may be `pi` or `opencode`; `request_id` is Pi's retained `responseId` or OpenCode's retained assistant message ID. The import requires the saved Berget provider, model and UTC timestamp to match, and rejects ambiguous, synthetic, missing or conflicting requests. Duplicate identical lines and repeated imports do not add emissions twice. The file is validated before any observation changes; an error aborts the entire import. Keep the JSONL file private as it may include source metadata outside TokenAtlas's allowlist.

Only the documented numeric fields, grid region, methodology identifier and version survive validation. The private report can show the region and methodology; shared reports omit potentially identifying region and arbitrary method strings. CSV/JSON exports follow the report's privacy mode. A missing emissions object stays unknown, not zero.

The CLI `report` JSON includes `environment` in totals, time buckets and groups, with the number of Berget requests and the number carrying estimates. The HTML card follows filters and excludes observations with uncertain request identities from its sums. CSV exports include each published environmental field as its own column; JSON exports retain the nested shape. Shared exports leave region and methodology strings blank.

## Interpretation

The calculator uses modeled workload and grid factors. Even when a provider reports a per-request result, it is not direct metering of that single request. TokenAtlas labels the source and keeps its aggregation coverage visible. Do not add the provider's IT energy to TokenAtlas's token proxy, or apply Berget's model and grid assumptions to Claude, Codex or another opaque provider.

## Optional offline calculation

`tokenatlas calculate-emissions inputs.json` implements Berget's published calculator arithmetic in Python and prints a JSON `usage.emissions` object. `inputs.json` must have exactly four objects: `model`, `hardware`, `grid`, and `request`, with every field required by the corresponding dataclass in [`tokenatlas/berget_calculator.py`](../tokenatlas/berget_calculator.py). The command needs no Node runtime and makes no network request. The Python `calculate_breakdown(...)` API also returns the model's component breakdown and water estimate; water is deliberately outside the published `usage.emissions` shape.

Supply actual model size and parameter count, GPU/node power and embodied factors, grid intensity and demand curve, observed response time, token counts, utilization, concurrency and cache fraction. TokenAtlas has no implicit defaults for these inputs. Calculation produces a scenario estimate and does not turn an absent provider response into a provider-reported reading.
