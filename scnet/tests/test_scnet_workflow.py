from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scnet import create_station_output_jobs as generator
from scnet import monitor_station_output_jobs as monitor


class GeneratorTests(unittest.TestCase):
    def make_project(self, root: Path) -> Path:
        project = root / "project"
        project.mkdir()
        (project / generator.ENTRYPOINT).write_text("print('placeholder')\n", encoding="utf-8")
        stations = project / "data" / "stations"
        stations.mkdir(parents=True)
        for relative in generator.DEFAULT_CSVS:
            (project / relative).write_text(
                "year,type,lon,lat,capacity_gw\n2030,solar,0,0,1\n",
                encoding="utf-8",
            )
        return project

    def test_default_bcsd_generates_three_unique_valid_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            jobs = root / "jobs"
            logs = root / "logs"
            args = generator.build_parser().parse_args(
                [
                    "--model",
                    "CANESM5",
                    "--project-dir",
                    str(project),
                    "--jobs-dir",
                    str(jobs),
                    "--logs-dir",
                    str(logs),
                ]
            )
            scripts = generator.create_jobs(args)
            self.assertEqual(len(scripts), 3)
            self.assertEqual(len({path.name for path in scripts}), 3)
            for path in scripts:
                subprocess.run(["bash", "-n", str(path)], check=True)
                text = path.read_text(encoding="utf-8")
                self.assertIn("#SBATCH -N 1", text)
                self.assertIn("#SBATCH -n 4", text)
                self.assertIn(
                    "source /work/home/acbpgywfpz/miniconda3/bin/activate climate",
                    text,
                )
                self.assertIn("--source bcsd --model CANESM5", text)
                self.assertNotIn("--scenario", text)
                self.assertNotIn("--years", text)
                self.assertNotIn("sbatch", text)

    def test_dry_run_does_not_create_job_or_log_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            jobs = root / "jobs"
            logs = root / "logs"
            args = generator.build_parser().parse_args(
                [
                    "--model",
                    "NESM3",
                    "--project-dir",
                    str(project),
                    "--jobs-dir",
                    str(jobs),
                    "--logs-dir",
                    str(logs),
                    "--dry-run",
                ]
            )
            self.assertEqual(generator.create_jobs(args), [])
            self.assertFalse(jobs.exists())
            self.assertFalse(logs.exists())

    def test_year_selection_is_rejected(self) -> None:
        args = generator.build_parser().parse_args(
            ["--model", "NESM3", "--years", "2030"]
        )
        with self.assertRaisesRegex(ValueError, "不允许 --years"):
            generator.validate_args(args)

    def test_nam12_requires_and_passes_native_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            args = generator.build_parser().parse_args(
                [
                    "--source",
                    "nam12",
                    "--gcm",
                    "CanESM5",
                    "--realization",
                    "r1i1p2f1",
                    "--rcm",
                    "CRCM5",
                    "--project-dir",
                    str(project),
                    "--jobs-dir",
                    str(root / "jobs"),
                    "--logs-dir",
                    str(root / "logs"),
                ]
            )
            scripts = generator.create_jobs(args)
            text = scripts[0].read_text(encoding="utf-8")
            self.assertIn("--source nam12", text)
            self.assertIn("--gcm CanESM5", text)
            self.assertIn("--realization r1i1p2f1", text)
            self.assertIn("--rcm CRCM5", text)


class MonitorClassificationTests(unittest.TestCase):
    def test_completed_requires_valid_output_contract(self) -> None:
        unit = {
            "scheduler": {"state": "COMPLETED", "exit_code": "0:0"},
            "summary": {"valid": False, "error": "输出缺失"},
        }
        self.assertEqual(monitor.classify_unit(unit), ("incomplete_output", "输出缺失"))

    def test_active_takes_precedence_over_missing_output(self) -> None:
        unit = {
            "scheduler": {"state": "RUNNING"},
            "summary": {"valid": False, "error": "运行汇总 CSV 不存在"},
        }
        classification, _ = monitor.classify_unit(unit)
        self.assertEqual(classification, "active")

    def test_success_requires_all_evidence(self) -> None:
        unit = {
            "scheduler": {"state": "COMPLETED", "exit_code": "0:0"},
            "summary": {"valid": True},
        }
        classification, _ = monitor.classify_unit(unit)
        self.assertEqual(classification, "succeeded")

    def test_bad_summary_rows_are_deterministic_failure(self) -> None:
        unit = {
            "scheduler": {"state": "COMPLETED", "exit_code": "0:0"},
            "summary": {
                "valid": False,
                "bad_rows": [{"region": "India", "type": "solar", "status": "no_cf"}],
                "error": "汇总含 no_cf、no_shape 或未知状态",
            },
        }
        classification, _ = monitor.classify_unit(unit)
        self.assertEqual(classification, "deterministic_failure")


if __name__ == "__main__":
    unittest.main()
