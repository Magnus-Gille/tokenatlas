"""Offline adapter for Berget AI's published per-request CO2 schema.

This module is deliberately an explicit-input calculator.  It does not contain
provider, model, hardware, grid, concurrency, or cache defaults, because
silently selecting those values would turn an estimate into an unsupported
claim about an opaque provider.  Callers must supply the measurements and
assumptions they want to use.

The arithmetic mirrors Berget AI's MIT-licensed ``co2-calculator`` package
(``calculator.ts`` and ``api-emissions.ts``).  The API returned by
``calculate_emissions`` is the published ``usage.emissions`` shape, with
training excluded from the consumer-inference boundary.  The accompanying
``CalculatorResult`` retains the component breakdown and water estimate for
offline reports; water is intentionally not added to the published API shape.

Upstream references (checked 2026-10-24):

* https://github.com/berget-ai/co2-calculator/blob/main/packages/co2-calculator/src/calculator.ts
* https://github.com/berget-ai/co2-calculator/blob/main/packages/co2-calculator/src/api-emissions.ts
* https://github.com/berget-ai/co2-calculator/blob/main/METHODOLOGY.md

The calculator is an estimate based on explicit deployment inputs.  It is not
a meter reading, and it must not be used to fill in missing provider metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


CALCULATOR_VERSION = "1.0.0"
METHODOLOGY_URL = (
    "https://github.com/berget-ai/co2-calculator/blob/main/METHODOLOGY.md"
)
SECONDS_PER_HOUR = 3_600.0


@dataclass(frozen=True)
class ModelInputs:
    """Model facts required by Berget's memory and token-time formulas."""

    parameters: float
    model_size_bytes: float
    default_input_tokens: float
    default_output_tokens: float
    min_gpus: float


@dataclass(frozen=True)
class HardwareInputs:
    """Node measurements and lifecycle assumptions, all in source units."""

    gpu_count: int
    gpu_memory_gb: float
    node_idle_watts: float
    node_peak_watts: float
    embodied_per_gpu_kg: float
    other_compute_embodied_kg: float
    chassis_watts: float
    lifetime_years: float
    projected_lifetime_utilization: float


@dataclass(frozen=True)
class GridInputs:
    """Grid factors needed for time-of-day carbon and cooling accounting."""

    region: str
    intensity_gco2e_per_kwh: float
    demand_curve: tuple[float, ...]
    low_period_factor: float
    peak_period_factor: float
    low_period_threshold: float
    pue: float
    water_liters_per_kwh: float


@dataclass(frozen=True)
class RequestInputs:
    """Per-request measurements and serving assumptions."""

    measured_response_time_seconds: float
    input_tokens: float
    output_tokens: float
    hour_of_day: int
    gpu_concurrency: float
    node_concurrency: float
    lifetime_utilization: float
    gpu_compute_utilization: float
    cached_prompt_fraction: float
    caching_enabled: bool


@dataclass(frozen=True)
class CalculatorResult:
    """Detailed result plus the exact published API projection."""

    emissions: Mapping[str, Any]
    components: Mapping[str, Mapping[str, Any]]
    water_liters: float
    gpus_allocated: int
    effective_intensity_gco2e_per_kwh: float

    def as_api_emissions(self) -> dict[str, Any]:
        """Return a mutable, JSON-ready copy of the published shape."""

        return dict(self.emissions)


def _number(name: str, value: Any, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be >= {minimum:g}")
    return result


def _fraction(name: str, value: Any, *, allow_zero: bool = True) -> float:
    result = _number(name, value)
    lower = 0.0 if allow_zero else math.nextafter(0.0, 1.0)
    if result < lower or result > 1.0:
        bound = "(0, 1]" if not allow_zero else "[0, 1]"
        raise ValueError(f"{name} must be in {bound}")
    return result


def _validate(model: ModelInputs, hardware: HardwareInputs, grid: GridInputs, request: RequestInputs) -> None:
    _number("model.parameters", model.parameters, minimum=1)
    _number("model.model_size_bytes", model.model_size_bytes, minimum=1)
    _number("model.default_input_tokens", model.default_input_tokens, minimum=0)
    _number("model.default_output_tokens", model.default_output_tokens, minimum=0)
    if model.default_input_tokens + model.default_output_tokens <= 0:
        raise ValueError("model default token count must be > 0")
    _number("model.min_gpus", model.min_gpus, minimum=0)

    if isinstance(hardware.gpu_count, bool) or not isinstance(hardware.gpu_count, int):
        raise ValueError("hardware.gpu_count must be an integer >= 1")
    if hardware.gpu_count < 1:
        raise ValueError("hardware.gpu_count must be an integer >= 1")
    _number("hardware.gpu_memory_gb", hardware.gpu_memory_gb, minimum=0.000001)
    _number("hardware.node_idle_watts", hardware.node_idle_watts, minimum=0)
    peak = _number("hardware.node_peak_watts", hardware.node_peak_watts, minimum=0)
    if peak < hardware.node_idle_watts:
        raise ValueError("hardware.node_peak_watts must be >= node_idle_watts")
    _number("hardware.embodied_per_gpu_kg", hardware.embodied_per_gpu_kg, minimum=0)
    _number("hardware.other_compute_embodied_kg", hardware.other_compute_embodied_kg, minimum=0)
    _number("hardware.chassis_watts", hardware.chassis_watts, minimum=0)
    _number("hardware.lifetime_years", hardware.lifetime_years, minimum=0.000001)
    _fraction("hardware.projected_lifetime_utilization", hardware.projected_lifetime_utilization, allow_zero=False)

    if not isinstance(grid.region, str) or not grid.region.strip():
        raise ValueError("grid.region must be a non-empty string")
    _number("grid.intensity_gco2e_per_kwh", grid.intensity_gco2e_per_kwh, minimum=0)
    if not isinstance(grid.demand_curve, (tuple, list)) or len(grid.demand_curve) != 24:
        raise ValueError("grid.demand_curve must contain 24 hourly values")
    for hour, weight in enumerate(grid.demand_curve):
        _number(f"grid.demand_curve[{hour}]", weight, minimum=0)
    _number("grid.low_period_factor", grid.low_period_factor, minimum=0)
    _number("grid.peak_period_factor", grid.peak_period_factor, minimum=0)
    _number("grid.low_period_threshold", grid.low_period_threshold, minimum=0)
    _number("grid.pue", grid.pue, minimum=1)
    _number("grid.water_liters_per_kwh", grid.water_liters_per_kwh, minimum=0)

    _number("request.measured_response_time_seconds", request.measured_response_time_seconds, minimum=0)
    _number("request.input_tokens", request.input_tokens, minimum=0)
    _number("request.output_tokens", request.output_tokens, minimum=0)
    if isinstance(request.hour_of_day, bool) or not isinstance(request.hour_of_day, int):
        raise ValueError("request.hour_of_day must be an integer from 0 to 23")
    if request.hour_of_day < 0 or request.hour_of_day > 23:
        raise ValueError("request.hour_of_day must be an integer from 0 to 23")
    _number("request.gpu_concurrency", request.gpu_concurrency, minimum=1)
    _number("request.node_concurrency", request.node_concurrency, minimum=1)
    _fraction("request.lifetime_utilization", request.lifetime_utilization, allow_zero=False)
    _fraction("request.gpu_compute_utilization", request.gpu_compute_utilization)
    _fraction("request.cached_prompt_fraction", request.cached_prompt_fraction)
    if not isinstance(request.caching_enabled, bool):
        raise ValueError("request.caching_enabled must be a boolean")


def _calculate_breakdown(
    *,
    model: ModelInputs,
    hardware: HardwareInputs,
    grid: GridInputs,
    request: RequestInputs,
) -> CalculatorResult:
    """Calculate Berget-compatible emissions from explicit inputs.

    The result excludes model-training emissions, matching ``toApiEmissions``
    in Berget's published package.  This function performs no provider lookup
    and has no default model, hardware, grid, concurrency, cache, or timing
    values.
    """

    _validate(model, hardware, grid, request)

    bytes_per_param = model.model_size_bytes / model.parameters
    model_memory_gb = (model.parameters * bytes_per_param * 1.2) / (1024**3)
    weight_based_gpus = math.ceil(model_memory_gb / hardware.gpu_memory_gb)
    gpus_used = min(max(weight_based_gpus, math.floor(model.min_gpus)), hardware.gpu_count)

    weight = grid.demand_curve[request.hour_of_day]
    period_factor = grid.low_period_factor if weight <= grid.low_period_threshold else grid.peak_period_factor
    effective_intensity = grid.intensity_gco2e_per_kwh * period_factor

    cached_fraction = request.cached_prompt_fraction if request.caching_enabled else 0.0
    effective_input_tokens = request.input_tokens * (1.0 - cached_fraction)
    token_ratio = (effective_input_tokens + request.output_tokens) / (
        model.default_input_tokens + model.default_output_tokens
    )
    token_adjusted_seconds = request.measured_response_time_seconds * math.sqrt(token_ratio)
    if request.gpu_concurrency <= 8:
        concurrency_factor = 1.0
    else:
        concurrency_factor = 1.0 + math.log2(request.gpu_concurrency / 8.0) * 0.15
    gpu_time_seconds = token_adjusted_seconds * concurrency_factor
    gpu_time_hours = gpu_time_seconds / SECONDS_PER_HOUR

    idle_per_gpu_watts = hardware.node_idle_watts / hardware.gpu_count
    incremental_per_gpu_watts = (
        (hardware.node_peak_watts - hardware.node_idle_watts) / hardware.gpu_count
    ) * request.gpu_compute_utilization
    gpu_energy_kwh = (
        incremental_per_gpu_watts * gpu_time_hours * gpus_used / request.gpu_concurrency / 1_000
    )
    idle_energy_kwh = (
        idle_per_gpu_watts
        * gpu_time_hours
        * gpus_used
        / request.gpu_concurrency
        / request.lifetime_utilization
        / 1_000
    )
    server_energy_kwh = (
        hardware.chassis_watts
        * gpu_time_hours
        / request.node_concurrency
        / request.lifetime_utilization
        / 1_000
    )

    gpu_operational_co2 = gpu_energy_kwh * effective_intensity
    idle_operational_co2 = idle_energy_kwh * effective_intensity
    server_operational_co2 = server_energy_kwh * effective_intensity
    overhead_co2 = (
        gpu_operational_co2 + idle_operational_co2 + server_operational_co2
    ) * (grid.pue - 1.0)

    projected_active_seconds = (
        hardware.lifetime_years * 365.0 * 24.0 * SECONDS_PER_HOUR
    ) * hardware.projected_lifetime_utilization
    embodied_gpu_per_second = hardware.embodied_per_gpu_kg * 1_000.0 / projected_active_seconds
    embodied_gpu_co2 = (
        embodied_gpu_per_second * gpu_time_seconds * gpus_used / request.gpu_concurrency
    )
    embodied_other_per_second = (
        hardware.other_compute_embodied_kg * 1_000.0 / projected_active_seconds
    )
    embodied_other_co2 = (
        embodied_other_per_second * gpu_time_seconds / request.node_concurrency
    )

    def component(co2: float, energy: float, label: str) -> dict[str, Any]:
        return {
            "co2_grams": round(co2, 6),
            "energy_kwh": round(energy, 9),
            "label": label,
        }

    components = {
        "gpu_operational": component(gpu_operational_co2, gpu_energy_kwh, "GPU energy (compute)"),
        "gpu_idle": component(idle_operational_co2, idle_energy_kwh, "GPU idle baseline (standby)"),
        "server_operational": component(server_operational_co2, server_energy_kwh, "Server infrastructure"),
        "datacenter_overhead": component(
            overhead_co2, 0.0, f"Cooling & overhead (PUE {grid.pue:.2f})"
        ),
        "embodied_gpu": component(embodied_gpu_co2, 0.0, "GPU embodied (amortised)"),
        "embodied_other": component(embodied_other_co2, 0.0, "Other compute embodied (amortised)"),
    }
    operational_co2 = sum(
        components[name]["co2_grams"]
        for name in ("gpu_operational", "gpu_idle", "server_operational", "datacenter_overhead")
    )
    embodied_co2 = components["embodied_gpu"]["co2_grams"] + components["embodied_other"]["co2_grams"]
    total_energy_kwh = gpu_energy_kwh + idle_energy_kwh + server_energy_kwh
    emissions = {
        "co2e_grams": operational_co2 + embodied_co2,
        "energy_kwh": total_energy_kwh,
        "operational": {
            "co2e_grams": operational_co2,
            "energy_kwh": total_energy_kwh,
        },
        "embodied": {"co2e_grams": embodied_co2},
        "grid": {
            "region": grid.region,
            "carbon_intensity_gco2e_per_kwh": round(effective_intensity, 2),
        },
        "methodology": METHODOLOGY_URL,
        "methodology_version": CALCULATOR_VERSION,
    }
    return CalculatorResult(
        emissions=emissions,
        components=components,
        water_liters=total_energy_kwh * grid.water_liters_per_kwh,
        gpus_allocated=gpus_used,
        effective_intensity_gco2e_per_kwh=round(effective_intensity, 2),
    )


def calculate_breakdown(
    *,
    model: ModelInputs,
    hardware: HardwareInputs,
    grid: GridInputs,
    request: RequestInputs,
) -> CalculatorResult:
    """Calculate from explicit inputs and reject nonfinite derived accounting."""
    try:
        result = _calculate_breakdown(model=model, hardware=hardware, grid=grid, request=request)
    except (OverflowError, ZeroDivisionError) as exc:
        raise ValueError("calculated emissions must be finite; check input magnitudes") from exc
    numbers = [result.water_liters, result.effective_intensity_gco2e_per_kwh,
               result.emissions['co2e_grams'], result.emissions['energy_kwh'],
               result.emissions['operational']['co2e_grams'], result.emissions['operational']['energy_kwh'],
               result.emissions['embodied']['co2e_grams'],
               result.emissions['grid']['carbon_intensity_gco2e_per_kwh']]
    for component in result.components.values():
        numbers.extend((component['co2_grams'], component['energy_kwh']))
    if not all(math.isfinite(value) for value in numbers):
        raise ValueError("calculated emissions must be finite; check input magnitudes")
    return result


def calculate_emissions(
    *,
    model: ModelInputs,
    hardware: HardwareInputs,
    grid: GridInputs,
    request: RequestInputs,
) -> dict[str, Any]:
    """Return only the published Berget ``ApiEmissions`` mapping."""

    return calculate_breakdown(
        model=model, hardware=hardware, grid=grid, request=request
    ).as_api_emissions()


__all__ = [
    "CALCULATOR_VERSION",
    "METHODOLOGY_URL",
    "CalculatorResult",
    "GridInputs",
    "HardwareInputs",
    "ModelInputs",
    "RequestInputs",
    "calculate_breakdown",
    "calculate_emissions",
]
