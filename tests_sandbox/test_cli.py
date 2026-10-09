import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests_sandbox.fixtures import warehouse


ROOT = Path(__file__).resolve().parents[1]


class CommandLineUserFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="warehouse-cli-test-")
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)

    def invoke(self, *arguments):
        return subprocess.run([sys.executable, "-m", "warehouse_sandbox.cli", *map(str, arguments)],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)

    def test_command_line_output_is_reusable_input_and_restores_settings(self):
        source = self.folder / "warehouse.json"
        settings = self.folder / "settings.json"
        first_path = self.folder / "first.json"
        second_path = self.folder / "second.json"
        source.write_text(json.dumps(warehouse()))
        settings.write_text(json.dumps({"forklift_count": 2, "adoption_percent": 0, "seed": 739, "speed_mps": 1.5}))
        first = self.invoke("--input", source, "--settings", settings, "--output", first_path)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.invoke("--input", first_path, "--output", second_path)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(first_path.read_text()), json.loads(second_path.read_text()))

    def test_explicit_settings_override_an_imported_scenario(self):
        from warehouse_sandbox.engine import run_comparison
        from warehouse_sandbox.importer import validate_dataset

        dataset = validate_dataset(warehouse())
        saved = run_comparison(dataset, {"adoption_percent": 0, "seed": 739})
        saved["dataset"] = dataset
        source = self.folder / "scenario.json"
        settings = self.folder / "settings.json"
        output = self.folder / "result.json"
        source.write_text(json.dumps(saved))
        settings.write_text(json.dumps({"adoption_percent": 100, "seed": 42}))
        result = self.invoke("--input", source, "--settings", settings, "--output", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(output.read_text())
        self.assertEqual(parsed["settings"]["adoption_percent"], 100)
        self.assertEqual(parsed["settings"]["seed"], 42)
        self.assertTrue(any(proposal["adopted"] for proposal in parsed["proposed"]["suggestions"]))

    def test_invalid_command_line_input_reports_failure_without_traceback_or_output(self):
        source = self.folder / "bad.json"
        output = self.folder / "result.json"
        source.write_text("{broken json")
        result = self.invoke("--input", source, "--output", output)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(output.exists())

    def test_command_line_template_csv_directory_is_a_working_entrypoint(self):
        output = self.folder / "result.json"
        result = self.invoke("--csv-dir", ROOT / "examples" / "sandbox" / "csv", "--output", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(output.read_text())
        self.assertEqual(parsed["baseline"]["metrics"]["truck_count"], len(parsed["dataset"]["orders"]))
        self.assertEqual(parsed["proposed"]["metrics"]["truck_count"], len(parsed["dataset"]["orders"]))


if __name__ == "__main__":
    unittest.main()
