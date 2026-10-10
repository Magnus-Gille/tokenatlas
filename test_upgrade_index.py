import io
import unittest
from unittest.mock import patch

from tokenatlas import upgrade_index


def release_file(version, *, yanked=False, requires_python=None, package_type="bdist_wheel"):
    return {
        "filename": f"tokenatlas-{version}-py3-none-any.whl",
        "packagetype": package_type,
        "yanked": yanked,
        "requires_python": requires_python,
    }


def index(releases):
    # Deliberately misleading: selection must use releases, not info.version.
    return {"info": {"version": "999.0.0"}, "releases": releases}


class VersionValidationTests(unittest.TestCase):
    def test_accepts_numeric_three_component_versions(self):
        for value in ("0.0.0", "1.2.3", "10.20.300"):
            with self.subTest(value=value):
                self.assertEqual(upgrade_index.validate_version(value), value)

    def test_rejects_non_numeric_or_non_stable_versions(self):
        for value in (
            "1", "1.2", "1.2.3.4", "01.2.3", "1.02.3", "1.2.03", "1.2.3rc1",
            "v1.2.3", "1.2.3+local", "https://pypi.org/project/tokenatlas/1.2.3/",
            "--index-url=https://example.test", "", None, 123,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                upgrade_index.validate_version(value)

    def test_rejects_unreasonably_large_numeric_components(self):
        with self.assertRaises(ValueError):
            upgrade_index.validate_version("1234567890.1.2")


class LatestVersionTests(unittest.TestCase):
    def test_python_exact_match_pads_trailing_zero_components(self):
        self.assertTrue(upgrade_index._python_compatible("==3.10", (3, 10, 0)))
        self.assertFalse(upgrade_index._python_compatible("==3.10", (3, 10, 1)))
        self.assertTrue(upgrade_index._python_compatible("!=3.10", (3, 10, 1)))
        self.assertFalse(upgrade_index._python_compatible("!=3.10", (3, 10, 0)))

    def test_arbitrary_python_equality_is_an_exact_string_match(self):
        self.assertTrue(upgrade_index._python_compatible("===3.10.0", (3, 10, 0)))
        self.assertFalse(upgrade_index._python_compatible("===3.10", (3, 10, 0)))

    def test_selects_highest_stable_compatible_release_by_numeric_order(self):
        result = upgrade_index.latest_version(fetch_json=lambda url: index({
            "1.9.0": [release_file("1.9.0")],
            "1.10.0": [release_file("1.10.0")],
            "1.11.0rc1": [release_file("1.11.0rc1")],
        }))
        self.assertEqual(result, "1.10.0")

    def test_skips_yanked_files_and_releases_without_files(self):
        result = upgrade_index.latest_version(fetch_json=lambda url: index({
            "2.0.0": [release_file("2.0.0", yanked=True)],
            "1.9.0": [],
            "1.8.0": [release_file("1.8.0", package_type="bdist_egg")],
            "1.7.0": [release_file("1.7.0", package_type="sdist")],
        }))
        self.assertEqual(result, "1.7.0")

    def test_skips_releases_incompatible_with_running_python(self):
        # The current interpreter is >=3.10 per project support. Keeping the
        # constraint relative makes this test deterministic on all supported CI.
        result = upgrade_index.latest_version(fetch_json=lambda url: index({
            "3.0.0": [release_file("3.0.0", requires_python=">=99.0")],
            "2.0.0": [release_file("2.0.0", requires_python=">=3.10,<99.0")],
            "1.0.0": [release_file("1.0.0", requires_python="~=3.10")],  # unsupported syntax fails closed
        }))
        self.assertEqual(result, "2.0.0")

    def test_returns_actionable_error_when_no_compatible_release_exists(self):
        with self.assertRaisesRegex(ValueError, "no stable TokenAtlas release"):
            upgrade_index.latest_version(fetch_json=lambda url: index({
                "1.0.0": [release_file("1.0.0", yanked=True)],
            }))

    def test_rejects_malformed_index_and_file_metadata(self):
        with self.assertRaisesRegex(ValueError, "invalid version index"):
            upgrade_index.latest_version(fetch_json=lambda url: {"info": {"version": "1.0.0"}})
        with self.assertRaisesRegex(ValueError, "invalid release file metadata"):
            upgrade_index.latest_version(fetch_json=lambda url: index({"1.0.0": [{"filename": "bad"}]}))

    def test_network_failure_is_concise_and_does_not_echo_error_data(self):
        with patch.object(upgrade_index.urllib.request, "urlopen", side_effect=OSError("secret response body")):
            with self.assertRaises(ValueError) as caught:
                upgrade_index.latest_version()
        self.assertIn("check the connection", str(caught.exception))
        self.assertNotIn("secret response body", str(caught.exception))

    def test_oversized_response_is_rejected(self):
        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        body = b"{" + b" " * upgrade_index._MAX_RESPONSE_BYTES
        with patch.object(upgrade_index.urllib.request, "urlopen", return_value=FakeResponse(body)):
            with self.assertRaisesRegex(ValueError, "exceeded the 1 MiB size limit"):
                upgrade_index.latest_version()

    def test_default_fetch_uses_fixed_https_endpoint_and_timeout(self):
        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        data = b'{"info":{"version":"99.0.0"},"releases":{"1.2.3":[{"filename":"x.whl","packagetype":"bdist_wheel","yanked":false,"requires_python":null}]}}'
        with patch.object(upgrade_index.urllib.request, "urlopen", return_value=FakeResponse(data)) as opened:
            self.assertEqual(upgrade_index.latest_version(), "1.2.3")
        (request,) = opened.call_args.args
        self.assertEqual(request.full_url, upgrade_index.PYPI_JSON_URL)
        self.assertEqual(opened.call_args.kwargs["timeout"], upgrade_index._TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
