#!/usr/bin/env python3
"""从本地只读采集 SCNet 场站出力作业状态。

监控器每 30 分钟通过 SSH 查询 Slurm、少量日志尾部、运行汇总 CSV 和预期
输出文件是否存在。它只写本地状态记录，不提交、取消、重试或修改远程文件。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_SCENARIOS = ("ssp126", "ssp245", "ssp585")
ACTIVE_STATES = {
    "CONFIGURING",
    "COMPLETING",
    "PENDING",
    "RUNNING",
    "RESIZING",
    "REQUEUED",
    "REQUEUE_FED",
    "SIGNALING",
    "STAGE_OUT",
    "SUSPENDED",
}
RETRYABLE_STATES = {"BOOT_FAIL", "NODE_FAIL", "PREEMPTED", "REVOKED", "TIMEOUT"}
RESOURCE_STATES = {"OUT_OF_MEMORY"}


REMOTE_COLLECTOR = r'''
from __future__ import print_function

import argparse
import csv
import getpass
import json
import os
import re
import subprocess
from pathlib import Path


def run(command):
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    result = subprocess.run(
        command,
        universal_newlines=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr.strip(),
    }


def normalize_state(value):
    return value.strip().split(" ", 1)[0].rstrip("+")


def job_number(value):
    match = re.match(r"^(\d+)", value)
    return int(match.group(1)) if match else -1


def tail(path, max_lines=30, max_chars=12000):
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])[-max_chars:]


def expected_output(args, row, scenario, output_root):
    stype = row.get("type", "")
    region = row.get("region", "")
    prefix = "pv" if stype == "solar" else "wind"
    if args.source == "nam12":
        return (
            output_root
            / (prefix + "_out_NAM-12")
            / args.gcm
            / args.realization
            / (
                prefix
                + "_stations_out_NAM-12_"
                + args.gcm
                + "_"
                + args.realization
                + "_"
                + args.rcm
                + "_"
                + scenario
                + "_allmonths.nc"
            )
        )
    model = args.model
    return (
        output_root
        / (prefix + "_out")
        / model
        / region
        / (
            prefix
            + "_stations_out_"
            + region
            + "_"
            + model
            + "_"
            + scenario
            + "_allmonths.nc"
        )
    )


def check_summary(args, scenario, project_dir, output_root):
    identity = args.gcm if args.source == "nam12" else args.model
    summary = output_root / (
        "run_summary_" + args.source + "_" + identity + "_" + scenario + ".csv"
    )
    result = {
        "path": str(summary),
        "exists": summary.is_file(),
        "valid": False,
        "rows": 0,
        "status_counts": {},
        "missing_outputs": [],
        "bad_rows": [],
        "error": "",
    }
    if not summary.is_file():
        result["error"] = "运行汇总 CSV 不存在"
        return result
    try:
        with summary.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception as exc:
        result["error"] = "无法读取运行汇总 CSV: {}".format(exc)
        return result
    result["rows"] = len(rows)
    if not rows:
        result["error"] = "运行汇总 CSV 没有数据行"
        return result

    acceptable = {"ok", "exists", "no_stations"}
    for row in rows:
        status = row.get("status", "")
        result["status_counts"][status] = result["status_counts"].get(status, 0) + 1
        if status not in acceptable:
            result["bad_rows"].append(
                {
                    "region": row.get("region", ""),
                    "type": row.get("type", ""),
                    "status": status,
                }
            )
            continue
        if status in {"ok", "exists"}:
            output = expected_output(args, row, scenario, output_root)
            if not output.is_file():
                result["missing_outputs"].append(str(output))
    result["valid"] = not result["bad_rows"] and not result["missing_outputs"]
    if result["bad_rows"]:
        result["error"] = "汇总含 no_cf、no_shape 或未知状态"
    elif result["missing_outputs"]:
        result["error"] = "汇总要求的输出文件缺失"
    return result


parser = argparse.ArgumentParser()
parser.add_argument("--source", required=True)
parser.add_argument("--model", default="")
parser.add_argument("--gcm", default="")
parser.add_argument("--realization", default="")
parser.add_argument("--rcm", default="")
parser.add_argument("--project-dir", required=True)
parser.add_argument("--logs-dir", required=True)
parser.add_argument("--output-dir", required=True)
parser.add_argument("--history-start", required=True)
parser.add_argument("--scenarios", nargs="+", required=True)
args = parser.parse_args()

project_dir = Path(os.path.expanduser(args.project_dir)).resolve()
logs_dir = Path(os.path.expanduser(args.logs_dir)).resolve()
output_root = Path(os.path.expanduser(args.output_dir))
if not output_root.is_absolute():
    output_root = project_dir / output_root
output_root = output_root.resolve()
identity = args.gcm if args.source == "nam12" else args.model
job_names = {
    scenario: "stout_{}_{}_{}".format(args.source, identity, scenario)
    for scenario in args.scenarios
}

squeue = run([
    "squeue", "-h", "-u", getpass.getuser(),
    "-o", "%i|%j|%T|%M|%R",
])
sacct = run([
    "sacct", "-X", "-n", "-P", "-u", getpass.getuser(),
    "-S", args.history_start,
    "-o", "JobIDRaw,JobName%100,State,ExitCode,Elapsed,AllocCPUS,ReqMem",
])
sacct_steps = run([
    "sacct", "-n", "-P", "-u", getpass.getuser(),
    "-S", args.history_start,
    "-o", "JobIDRaw,State,MaxRSS",
])

active_by_name = {}
if squeue["returncode"] == 0:
    for line in squeue["stdout"].splitlines():
        fields = line.split("|", 4)
        if len(fields) != 5 or fields[1] not in job_names.values():
            continue
        row = {
            "job_id": fields[0],
            "job_name": fields[1],
            "state": normalize_state(fields[2]),
            "elapsed": fields[3],
            "reason": fields[4],
        }
        previous = active_by_name.get(fields[1])
        if previous is None or job_number(row["job_id"]) > job_number(previous["job_id"]):
            active_by_name[fields[1]] = row

accounting_by_name = {}
if sacct["returncode"] == 0:
    for line in sacct["stdout"].splitlines():
        fields = line.split("|")
        if len(fields) < 7 or fields[1] not in job_names.values() or "." in fields[0]:
            continue
        row = {
            "job_id": fields[0],
            "job_name": fields[1],
            "state": normalize_state(fields[2]),
            "exit_code": fields[3],
            "elapsed": fields[4],
            "alloc_cpus": fields[5],
            "req_mem": fields[6],
        }
        previous = accounting_by_name.get(fields[1])
        if previous is None or job_number(row["job_id"]) > job_number(previous["job_id"]):
            accounting_by_name[fields[1]] = row

max_rss_by_job = {}
if sacct_steps["returncode"] == 0:
    for line in sacct_steps["stdout"].splitlines():
        fields = line.split("|")
        if len(fields) < 3 or not fields[0].endswith(".batch") or not fields[2]:
            continue
        max_rss_by_job[fields[0][:-6]] = fields[2]
for collection in (active_by_name, accounting_by_name):
    for row in collection.values():
        row["max_rss"] = max_rss_by_job.get(row["job_id"], "")

units = []
for scenario in args.scenarios:
    job_name = job_names[scenario]
    scheduler = active_by_name.get(job_name) or accounting_by_name.get(job_name)
    job_id = scheduler.get("job_id", "") if scheduler else ""
    stdout_path = logs_dir / (job_name + "_" + job_id + ".out") if job_id else None
    stderr_path = logs_dir / (job_name + "_" + job_id + ".err") if job_id else None
    stdout_tail = tail(stdout_path) if stdout_path else ""
    stderr_tail = tail(stderr_path) if stderr_path else ""
    units.append(
        {
            "scenario": scenario,
            "job_name": job_name,
            "scheduler": scheduler,
            "summary": check_summary(args, scenario, project_dir, output_root),
            "stdout_path": str(stdout_path) if stdout_path else "",
            "stderr_path": str(stderr_path) if stderr_path else "",
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "done_marker": "[STATION_OUTPUT_DONE]" in stdout_tail,
        }
    )

print(json.dumps({
    "remote_home": str(Path.home()),
    "project_dir": str(project_dir),
    "logs_dir": str(logs_dir),
    "output_root": str(output_root),
    "squeue_error": squeue["stderr"] if squeue["returncode"] else "",
    "sacct_error": sacct["stderr"] if sacct["returncode"] else "",
    "sacct_steps_error": sacct_steps["stderr"] if sacct_steps["returncode"] else "",
    "units": units,
}, ensure_ascii=True))
'''


def safe_token(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError(f"{label} 含不安全字符：{value!r}")
    return value


def server_token(server: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", server).strip("._-")
    if not token:
        raise ValueError(f"无法生成服务器标识：{server!r}")
    return token


def normalize_state(state: str) -> str:
    return state.strip().split(" ", 1)[0].rstrip("+")


def build_parser() -> argparse.ArgumentParser:
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="每 30 分钟只读检查远程场站出力作业并写入本地状态记录"
    )
    parser.add_argument("--server", nargs="+", required=True, help="一个或多个 SSH 主机名")
    parser.add_argument(
        "--source",
        choices=("bcsd", "china", "nam12"),
        default="bcsd",
        help="数据源（默认：bcsd）",
    )
    parser.add_argument("--model", help="bcsd/china 模型名")
    parser.add_argument("--gcm", help="NAM-12 GCM")
    parser.add_argument("--realization", help="NAM-12 realization")
    parser.add_argument("--rcm", help="NAM-12 RCM")
    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=list(DEFAULT_SCENARIOS),
        help="需要监控的情景（默认：ssp126 ssp245 ssp585）",
    )
    parser.add_argument(
        "--project-dir",
        default="~/calculate_wind_solar_out",
        help="远程仓库目录",
    )
    parser.add_argument(
        "--logs-dir",
        default="~/logs/station_output_0p1deg",
        help="远程 Slurm 日志目录",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/outputs_0p1deg",
        help="远程科学输出目录；相对路径以远程仓库为基准",
    )
    parser.add_argument(
        "--status-root",
        default=None,
        help="本地状态根目录（默认：scnet/models/<MODEL>/completion_status）",
    )
    parser.add_argument("--interval", type=int, default=1800, help="检查间隔秒数（默认：1800）")
    parser.add_argument("--once", action="store_true", help="只检查一次")
    parser.add_argument("--history-days", type=int, default=30, help="sacct 回看天数")
    parser.add_argument("--ssh-timeout", type=int, default=180, help="单台服务器 SSH 超时秒数")
    parser.set_defaults(repository_root=repository_root)
    return parser


def validate_args(args: argparse.Namespace) -> str:
    if args.interval <= 0 or args.history_days <= 0 or args.ssh_timeout <= 0:
        raise ValueError("interval、history-days 和 ssh-timeout 必须是正整数")
    if args.source in {"bcsd", "china"}:
        if not args.model:
            raise ValueError(f"{args.source} 监控需要 --model")
        identity = safe_token(args.model, "model")
    else:
        missing = [name for name in ("gcm", "realization", "rcm") if not getattr(args, name)]
        if missing:
            raise ValueError("nam12 监控缺少参数：" + ", ".join(f"--{x}" for x in missing))
        identity = safe_token(args.gcm, "gcm")
        safe_token(args.realization, "realization")
        safe_token(args.rcm, "rcm")
    for scenario in args.scenarios:
        safe_token(scenario, "scenario")
    if len(set(args.scenarios)) != len(args.scenarios):
        raise ValueError("scenarios 不能重复")
    return identity


def remote_arguments(args: argparse.Namespace, history_start: str) -> list[str]:
    values = [
        "python3",
        "-",
        "--source",
        args.source,
        "--project-dir",
        args.project_dir,
        "--logs-dir",
        args.logs_dir,
        "--output-dir",
        args.output_dir,
        "--history-start",
        history_start,
        "--scenarios",
        *args.scenarios,
    ]
    for name in ("model", "gcm", "realization", "rcm"):
        value = getattr(args, name)
        if value:
            values.extend((f"--{name}", value))
    return values


def query_server(args: argparse.Namespace, server: str) -> dict[str, Any]:
    history_start = (datetime.now() - timedelta(days=args.history_days)).strftime("%Y-%m-%d")
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={min(args.ssh_timeout, 60)}",
        server,
        shlex.join(remote_arguments(args, history_start)),
    ]
    result = subprocess.run(
        command,
        input=REMOTE_COLLECTOR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=args.ssh_timeout,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"SSH 查询失败（退出码 {result.returncode}）：{message}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"远程返回不是有效 JSON：{exc}") from exc
    payload["server"] = server
    return payload


def classify_unit(unit: dict[str, Any]) -> tuple[str, str]:
    scheduler = unit.get("scheduler")
    summary = unit.get("summary", {})
    if scheduler is None:
        return "not_submitted", "未找到活动作业或近期 sacct 记录"
    state = normalize_state(scheduler.get("state", ""))
    if state in ACTIVE_STATES:
        return "active", f"Slurm 状态为 {state}"
    exit_code = scheduler.get("exit_code", "")
    if state == "COMPLETED" and (not exit_code or exit_code.startswith("0:")):
        if summary.get("valid"):
            return "succeeded", "Slurm 成功，汇总状态和预期输出均满足契约"
        if summary.get("bad_rows"):
            return "deterministic_failure", summary.get("error") or "汇总包含失败状态"
        return "incomplete_output", summary.get("error") or "输出证据不满足契约"
    if state in RESOURCE_STATES:
        return "resource_failure", f"Slurm 状态为 {state}"
    if state in RETRYABLE_STATES:
        return "retryable", f"Slurm 状态为 {state}"
    return "deterministic_failure", f"Slurm 状态为 {state or 'UNKNOWN'}，ExitCode={exit_code}"


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def status_root_path(args: argparse.Namespace, identity: str) -> Path:
    return (
        Path(args.status_root).expanduser().resolve()
        if args.status_root
        else args.repository_root / "scnet" / "models" / identity / "completion_status"
    )


def write_status(
    args: argparse.Namespace,
    identity: str,
    server: str,
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    status_root = status_root_path(args, identity)
    observed_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    records: list[dict[str, Any]] = []
    has_error = bool(
        payload.get("squeue_error")
        or payload.get("sacct_error")
        or payload.get("sacct_steps_error")
    )
    for unit in payload.get("units", []):
        classification, reason = classify_unit(unit)
        record = {
            "unit_id": unit["job_name"],
            "stage": "station_output_0p1deg",
            "server": server,
            "source": args.source,
            "model": args.model or args.gcm,
            "scenario": unit["scenario"],
            "job_id": (unit.get("scheduler") or {}).get("job_id", ""),
            "scheduler_state": (unit.get("scheduler") or {}).get("state", ""),
            "exit_code": (unit.get("scheduler") or {}).get("exit_code", ""),
            "elapsed": (unit.get("scheduler") or {}).get("elapsed", ""),
            "max_rss": (unit.get("scheduler") or {}).get("max_rss", ""),
            "classification": classification,
            "reason": reason,
            "summary": unit.get("summary", {}),
            "stdout_path": unit.get("stdout_path", ""),
            "stderr_path": unit.get("stderr_path", ""),
            "stdout_tail": unit.get("stdout_tail", ""),
            "stderr_tail": unit.get("stderr_tail", ""),
            "done_marker": unit.get("done_marker", False),
            "observed_at": observed_at,
            "next_action": "等待 Agent 判断" if classification not in {"active", "succeeded", "not_submitted"} else "",
        }
        records.append(record)
        unit_path = status_root / server_token(server) / f"{record['unit_id']}.json"
        atomic_write_text(unit_path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        if classification not in {"active", "succeeded", "not_submitted"}:
            has_error = True

    snapshot = {
        "server": server,
        "source": args.source,
        "model": args.model or args.gcm,
        "observed_at": observed_at,
        "squeue_error": payload.get("squeue_error", ""),
        "sacct_error": payload.get("sacct_error", ""),
        "sacct_steps_error": payload.get("sacct_steps_error", ""),
        "records": records,
    }
    suffix = server_token(server)
    atomic_write_text(
        status_root / f"latest_{suffix}.json",
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
    )
    lines = [
        f"# {server} / {args.source} / {args.model or args.gcm}",
        "",
        f"检查时间：{observed_at}",
        "",
        "| 单元 | Job ID | Slurm | 分类 | 原因 |",
        "|---|---:|---|---|---|",
    ]
    for record in records:
        lines.append(
            "| {unit_id} | {job_id} | {scheduler_state} | {classification} | {reason} |".format(
                **record
            )
        )
    atomic_write_text(status_root / f"progress_{suffix}.md", "\n".join(lines) + "\n")
    return records, has_error


def write_combined_progress(
    args: argparse.Namespace,
    identity: str,
    records: list[dict[str, Any]],
    monitor_errors: list[tuple[str, str]],
) -> None:
    """按技能进度契约更新统一的 ``completion_status/progress.md``。"""
    checked_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    counts: dict[str, int] = {}
    for record in records:
        classification = record["classification"]
        counts[classification] = counts.get(classification, 0) + 1
    total = len(records) + len(monitor_errors)
    lines = [
        "# SCNet 场站出力作业进度",
        "",
        f"- Last checked: `{checked_at}`",
        f"- Total: `{total}`",
        f"- Not submitted: `{counts.get('not_submitted', 0)}`",
        f"- Active: `{counts.get('active', 0)}`",
        f"- Succeeded: `{counts.get('succeeded', 0)}`",
        "- Retryable/resource failure: "
        f"`{counts.get('retryable', 0) + counts.get('resource_failure', 0)}`",
        "- Deterministic/incomplete: "
        f"`{counts.get('deterministic_failure', 0) + counts.get('incomplete_output', 0)}`",
        f"- Monitor error/unknown: `{len(monitor_errors) + counts.get('unknown', 0)}`",
        "",
        "| Server | Unit ID | Source | Model | Scenario | Job ID | Slurm state | "
        "Classification | Output evidence | Reason | Updated at | Next action |",
        "|---|---|---|---|---|---:|---|---|---|---|---|---|",
    ]
    for record in sorted(records, key=lambda item: (item["server"], item["scenario"])):
        summary = record.get("summary", {})
        evidence = (
            f"summary={summary.get('exists', False)}, "
            f"rows={summary.get('rows', 0)}, "
            f"missing={len(summary.get('missing_outputs', []))}"
        )
        lines.append(
            "| {server} | {unit_id} | {source} | {model} | {scenario} | {job_id} | "
            "{scheduler_state} | {classification} | {evidence} | {reason} | "
            "{observed_at} | {next_action} |".format(
                evidence=evidence,
                **record,
            )
        )
    for server, reason in sorted(monitor_errors):
        lines.append(
            f"| {server} | monitor | {args.source} | {args.model or args.gcm} | - | - | "
            f"- | unknown | query failed | {reason} | {checked_at} | 等待 Agent 判断 |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- 每行对应一个独立 SSP 作业单元；本表在每次完整检查后原子更新。",
            "- 监控器只记录和报告，不提交、取消、重试或修改远程输出。",
            "",
        ]
    )
    atomic_write_text(
        status_root_path(args, identity) / "progress.md",
        "\n".join(lines),
    )


def check_once(args: argparse.Namespace, identity: str) -> bool:
    any_error = False
    all_records: list[dict[str, Any]] = []
    monitor_errors: list[tuple[str, str]] = []
    for server in args.server:
        try:
            payload = query_server(args, server)
            records, has_error = write_status(args, identity, server, payload)
            all_records.extend(records)
            any_error = any_error or has_error
            print(f"[{server}] {len(records)} 个作业单元")
            for record in records:
                stream = sys.stderr if record["classification"] not in {
                    "active",
                    "succeeded",
                    "not_submitted",
                } else sys.stdout
                print(
                    f"  {record['unit_id']}: {record['classification']} - {record['reason']}",
                    file=stream,
                )
        except Exception as exc:
            any_error = True
            monitor_errors.append((server, str(exc)))
            print(f"[{server}] 监控失败：{exc}", file=sys.stderr)
    write_combined_progress(args, identity, all_records, monitor_errors)
    return any_error


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        identity = validate_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    while True:
        has_error = check_once(args, identity)
        if args.once:
            raise SystemExit(2 if has_error else 0)
        now = time.time()
        delay = args.interval - (now % args.interval)
        print(f"下次检查约在 {int(delay)} 秒后")
        time.sleep(delay)


if __name__ == "__main__":
    main()
