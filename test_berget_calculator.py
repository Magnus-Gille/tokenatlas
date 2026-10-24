import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from tokenatlas.berget_calculator import (
    CALCULATOR_VERSION,
    METHODOLOGY_URL,
    GridInputs,
    HardwareInputs,
    ModelInputs,
    RequestInputs,
    calculate_breakdown,
    calculate_emissions,
)


DEMAND_CURVE = (
    0.15,
    0.10,
    0.10,
    0.10,
    0.10,
    0.12,
    0.30,
    0.60,
    0.85,
    0.95,
    1.00,
    0.95,
    0.90,
    0.85,
    0.85,
    0.90,
    0.95,
    1.00,
    0.85,
    0.70,
    0.55,
    0.40,
    0.25,
    0.20,
)


def fixture(*, hour=14, caching=True):
    return dict(
        model=ModelInputs(
            parameters=24_000_000_000,
            model_size_bytes=48_000_000_000,
            default_input_tokens=800,
            default_output_tokens=133,
            min_gpus=0,
        ),
        hardware=HardwareInputs(
            gpu_count=8,
            gpu_memory_gb=141,
            node_idle_watts=800,
            node_peak_watts=6_500,
            embodied_per_gpu_kg=1_000,
            other_compute_embodied_kg=4_000,
            chassis_watts=1_200,
            lifetime_years=5,
            projected_lifetime_utilization=0.5,
        ),
        grid=GridInputs(
            region="sweden",
            intensity_gco2e_per_kwh=8,
            demand_curve=DEMAND_CURVE,
            low_period_factor=0.7,
            peak_period_factor=1.15,
            low_period_threshold=0.2,
            pue=1.15,
            water_liters_per_kwh=0,
        ),
        request=RequestInputs(
            measured_response_time_seconds=1.2,
            input_tokens=800,
            output_tokens=400,
            hour_of_day=hour,
            gpu_concurrency=8,
            node_concurrency=8,
            lifetime_utilization=0.7,
            gpu_compute_utilization=0.25,
            cached_prompt_fraction=0.67,
            caching_enabled=caching,
        ),
    )


class BergetCalculatorTests(unittest.TestCase):
    def test_golden_vector_matches_published_api_projection(self):
        result = calculate_breakdown(**fixture())

        # This vector uses the upstream calculator's base values: H200, Sweden,
        # 24B BF16 model, 800/400 tokens, concurrency 8, and 67% prefix cache.
        self.assertEqual(result.gpus_allocated, 1)
        self.assertAlmostEqual(result.emissions["co2e_grams"], 0.008782, places=9)
        self.assertAlmostEqual(result.emissions["energy_kwh"], 0.0000715407600794958, places=15)
        self.assertAlmostEqual(result.emissions["operational"]["co2e_grams"], 0.000757, places=9)
        self.assertAlmostEqual(result.emissions["embodied"]["co2e_grams"], 0.008025, places=9)
        self.assertEqual(result.emissions["grid"], {
            "region": "sweden",
            "carbon_intensity_gco2e_per_kwh": 9.2,
        })
        self.assertEqual(result.emissions["methodology"], METHODOLOGY_URL)
        self.assertEqual(result.emissions["methodology_version"], CALCULATOR_VERSION)

    def test_public_projection_has_only_the_api_emissions_fields(self):
        emissions = calculate_emissions(**fixture())
        self.assertEqual(set(emissions), {
            "co2e_grams",
            "energy_kwh",
            "operational",
            "embodied",
            "grid",
            "methodology",
            "methodology_version",
        })
        self.assertNotIn("water_liters", emissions)

    def test_water_is_retained_in_breakdown_but_not_api_schema(self):
        args = fixture()
        args["grid"] = GridInputs(
            region="test-grid",
            intensity_gco2e_per_kwh=300,
            demand_curve=DEMAND_CURVE,
            low_period_factor=1,
            peak_period_factor=1,
            low_period_threshold=0.2,
            pue=1.5,
            water_liters_per_kwh=0.8,
        )
        result = calculate_breakdown(**args)
        self.assertGreater(result.water_liters, 0)
        self.assertNotIn("water_liters", result.emissions)

    def test_cache_switch_changes_only_the_token_time_path(self):
        cached = calculate_breakdown(**fixture(caching=True))
        uncached = calculate_breakdown(**fixture(caching=False))
        self.assertLess(cached.emissions["energy_kwh"], uncached.emissions["energy_kwh"])
        self.assertLess(cached.emissions["co2e_grams"], uncached.emissions["co2e_grams"])

    def test_hourly_grid_factor_is_explicit_and_reproducible(self):
        day = calculate_breakdown(**fixture(hour=14))
        night = calculate_breakdown(**fixture(hour=2))
        self.assertEqual(day.emissions["grid"]["carbon_intensity_gco2e_per_kwh"], 9.2)
        self.assertEqual(night.emissions["grid"]["carbon_intensity_gco2e_per_kwh"], 5.6)

    def test_rejects_malformed_or_implicit_inputs(self):
        args = fixture()
        args["grid"] = GridInputs(
            region="sweden",
            intensity_gco2e_per_kwh=8,
            demand_curve=DEMAND_CURVE[:23],
            low_period_factor=0.7,
            peak_period_factor=1.15,
            low_period_threshold=0.2,
            pue=1.15,
            water_liters_per_kwh=0,
        )
        with self.assertRaisesRegex(ValueError, "24 hourly"):
            calculate_breakdown(**args)
        with self.assertRaises(TypeError):
            # Every field is intentionally required; no hidden provider defaults.
            ModelInputs(parameters=1, model_size_bytes=2, default_input_tokens=1, default_output_tokens=1)  # type: ignore[call-arg]

    def test_offline_cli_requires_explicit_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inputs.json'
            path.write_text(json.dumps({name: asdict(value) for name, value in fixture().items()}))
            command = [sys.executable, '-B', '-m', 'tokenatlas', 'calculate-emissions', str(path)]
            result = subprocess.run(command, text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout)['usage']['emissions'], calculate_emissions(**fixture()))
            path.write_text(json.dumps({'model': asdict(fixture()['model'])}))
            rejected = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(rejected.returncode, 2)
            self.assertIn('needs exactly', rejected.stderr)


if __name__ == "__main__":
    unittest.main()
