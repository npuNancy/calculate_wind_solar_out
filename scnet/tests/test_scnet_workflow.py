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

    def test_regions_loop_generates_scenario_times_region_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            jobs = root / "jobs"
            logs = root / "logs"
            args = generator.build_parser().parse_args(
                [
                    "--model",
                    "CANESM5",
                    "--regions",
                    "India",
                    "Australia",
                    "Brazil",
                    "--project-dir",
                    str(project),
                    "--jobs-dir",
                    str(jobs),
                    "--logs-dir",
                    str(logs),
                ]
            )
            scripts = generator.create_jobs(args)
            self.assertEqual(len(scripts), 9)
            self.assertEqual(len({path.name for path in scripts}), 9)
            expected_regions = {"India", "Australia", "Brazil"}
            expected_scenarios = {"ssp126", "ssp245", "ssp585"}
            for path in scripts:
                subprocess.run(["bash", "-n", str(path)], check=True)
                text = path.read_text(encoding="utf-8")
                self.assertIn("--source bcsd --model CANESM5", text)
                self.assertNotIn("--years", text)
                # 每个脚本恰好包含一个 --region <R>
                region_hits = [
                    region
                    for region in expected_regions
                    if f"--region {region}" in text
                ]
                self.assertEqual(len(region_hits), 1, text)
                # job_name 形如 stout_bcsd_CANESM5_<scenario>_<region>
                stem = path.stem
                parts = stem.split("_")
                self.assertEqual(parts[0], "stout")
                self.assertEqual(parts[1], "bcsd")
                self.assertEqual(parts[2], "CANESM5")
                self.assertIn(parts[3], expected_scenarios)
                self.assertIn(parts[4], expected_regions)

    def test_region_and_regions_are_mutually_exclusive(self) -> None:
        args = generator.build_parser().parse_args(
            ["--model", "CANESM5", "--region", "India", "--regions", "Brazil"]
        )
        with self.assertRaisesRegex(ValueError, "互斥"):
            generator.validate_args(args)

    def test_regions_dedup_is_rejected(self) -> None:
        args = generator.build_parser().parse_args(
            ["--model", "CANESM5", "--regions", "India", "India"]
        )
        with self.assertRaisesRegex(ValueError, "重复"):
            generator.validate_args(args)


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

    def test_combined_progress_contains_required_table(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = monitor.build_parser().parse_args(
                [
                    "--server",
                    "scnet-wuzhen-199",
                    "--model",
                    "CANESM5",
                    "--status-root",
                    temporary,
                    "--once",
                ]
            )
            record = {
                "server": "scnet-wuzhen-199",
                "unit_id": "stout_bcsd_CANESM5_ssp126",
                "source": "bcsd",
                "model": "CANESM5",
                "scenario": "ssp126",
                "region": "",
                "job_id": "41094622",
                "scheduler_state": "PENDING",
                "classification": "active",
                "summary": {"exists": False, "rows": 0, "missing_outputs": []},
                "reason": "Slurm 状态为 PENDING",
                "observed_at": "2026-07-22T16:00:00+08:00",
                "next_action": "",
            }
            monitor.write_combined_progress(args, "CANESM5", [record], [])
            progress = Path(temporary, "progress.md").read_text(encoding="utf-8")
            self.assertIn("Last checked", progress)
            self.assertIn("stout_bcsd_CANESM5_ssp126", progress)
            self.assertIn("| Server | Unit ID |", progress)

    def test_combined_progress_with_regions_orders_and_lists_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = monitor.build_parser().parse_args(
                [
                    "--server",
                    "scnet-wuzhen-199",
                    "--model",
                    "CANESM5",
                    "--status-root",
                    temporary,
                    "--once",
                ]
            )
            base = {
                "server": "scnet-wuzhen-199",
                "source": "bcsd",
                "model": "CANESM5",
                "job_id": "",
                "scheduler_state": "PENDING",
                "classification": "active",
                "summary": {"exists": False, "rows": 0, "missing_outputs": []},
                "reason": "Slurm 状态为 PENDING",
                "observed_at": "2026-07-23T16:00:00+08:00",
                "next_action": "",
            }
            records = [
                {**base, "unit_id": "stout_bcsd_CANESM5_ssp126_Australia",
                 "scenario": "ssp126", "region": "Australia"},
                {**base, "unit_id": "stout_bcsd_CANESM5_ssp126_India",
                 "scenario": "ssp126", "region": "India"},
                {**base, "unit_id": "stout_bcsd_CANESM5_ssp585_Brazil",
                 "scenario": "ssp585", "region": "Brazil"},
            ]
            monitor.write_combined_progress(args, "CANESM5", records, [])
            progress = Path(temporary, "progress.md").read_text(encoding="utf-8")
            self.assertIn("stout_bcsd_CANESM5_ssp126_India", progress)
            self.assertIn("stout_bcsd_CANESM5_ssp126_Australia", progress)
            self.assertIn("stout_bcsd_CANESM5_ssp585_Brazil", progress)
            # 排序键为 (scenario, region) 升序：ssp126 的两区域相邻，字母序 Australia<India；
            # ssp585 整体排在 ssp126 之后。用 unit_id（全文唯一）定位行顺序。
            idx_aus = progress.index("stout_bcsd_CANESM5_ssp126_Australia")
            idx_india = progress.index("stout_bcsd_CANESM5_ssp126_India")
            idx_bra = progress.index("stout_bcsd_CANESM5_ssp585_Brazil")
            self.assertLess(idx_aus, idx_india)
            self.assertLess(idx_india, idx_bra)


if __name__ == "__main__":
    unittest.main()
