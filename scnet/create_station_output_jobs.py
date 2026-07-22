#!/usr/bin/env python3
"""为 0.1° 场站出力计算生成 Slurm 作业脚本。

默认一次生成 SSP1-2.6、SSP2-4.5 和 SSP5-6.0 三个独立作业。本脚本
只生成作业文件，不会调用 ``sbatch``、读取 NetCDF 或执行科学计算。
"""

import argparse
import io
import os
import re
import shlex
import sys
import unicodedata
from pathlib import Path
from typing import List, NamedTuple, Optional, Sequence, Set, Tuple


ENTRYPOINT = "station_output_calculator_0p1deg.py"
DEFAULT_CSVS = (
    "data/stations/stations_SSP1-2.6.csv",
    "data/stations/stations_SSP2-4.5.csv",
    "data/stations/stations_SSP5-6.0.csv",
)
DEFAULT_SHP = "data/maps/natural_earth/ne_110m_admin_0_countries.shp"
DEFAULT_CFS_DIR = "data/cfs"
DEFAULT_OUTPUT_DIR = "outputs/outputs_0p1deg"
DEFAULT_JOBS_DIR = "~/jobs/station_output_0p1deg"
DEFAULT_LOGS_DIR = "~/logs/station_output_0p1deg"
DEFAULT_CONDA_ACTIVATE = "/work/home/acbpgywfpz/miniconda3/bin/activate"
DEFAULT_CONDA_ENVIRONMENT = "climate"
DEFAULT_MAX_DIST = 0.15
SCENARIO_BY_CSV_TOKEN = {
    "SSP1-2.6": "ssp126",
    "SSP2-4.5": "ssp245",
    "SSP5-6.0": "ssp585",
    "SSP5-8.5": "ssp585",
}


def configure_utf8_stdio() -> None:
    """兼容远程登录节点缺失 UTF-8 locale 时的 Python 3.6 标准输出。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        encoding = (getattr(stream, "encoding", "") or "").lower()
        if encoding in {"ascii", "ansi_x3.4-1968"} and hasattr(stream, "buffer"):
            setattr(
                sys,
                name,
                io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="backslashreplace"),
            )


class JobSpec(NamedTuple):
    """一个独立可重试的 model/source × SSP 作业。"""

    csv_argument: str
    scenario: str
    job_name: str
    script_path: Path
    command: Tuple[str, ...]


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return parsed


def expanded_path(value: str) -> Path:
    """展开用户目录并返回绝对路径，但不解引用符号链接。"""
    return Path(os.path.abspath(os.path.expanduser(value)))


def shell_join(arguments: Sequence[str]) -> str:
    """兼容 Python 3.6 的 ``shlex.join``。"""
    return " ".join(shlex.quote(argument) for argument in arguments)


def safe_token(value: str, label: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError(
            f"{label} 只能包含英文字母、数字、点、下划线和连字符，"
            "且必须以字母或数字开头"
        )
    return value


def unique_values(values: Sequence[str], label: str) -> List[str]:
    result = []  # type: List[str]
    seen = set()  # type: Set[str]
    for raw_value in values:
        value = raw_value.strip()
        if not value:
            raise ValueError(f"{label} 不能包含空值")
        if value in seen:
            raise ValueError(f"{label} 存在重复值：{value}")
        seen.add(value)
        result.append(value)
    if not result:
        raise ValueError(f"{label} 不能为空")
    return result


def job_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", ascii_value).strip("-._")
    if not token:
        raise ValueError(f"无法从名称生成安全作业标识：{value!r}")
    return token


def infer_scenario(csv_path: str) -> str:
    basename = Path(csv_path).name
    for token, scenario in SCENARIO_BY_CSV_TOKEN.items():
        if token in basename:
            return scenario
    raise ValueError(
        f"无法从 CSV 文件名 {basename!r} 推断情景；"
        f"支持的标识为：{', '.join(SCENARIO_BY_CSV_TOKEN)}"
    )


def resolve_project_input(project_dir: Path, value: str) -> Path:
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        path = project_dir / path
    return Path(os.path.abspath(path))


def build_parser() -> argparse.ArgumentParser:
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=(
            "一次生成三个 SSP 的 0.1° 场站出力 Slurm 作业；"
            "只生成脚本，不提交作业。"
        )
    )

    # 与科学入口同名的参数。--csv 扩展为可传多个值，以支持一次生成三个 SSP。
    parser.add_argument(
        "--csv",
        nargs="+",
        default=list(DEFAULT_CSVS),
        metavar="CSV",
        help="一个或多个场站 CSV（默认使用三个 SSP CSV）",
    )
    parser.add_argument(
        "--source",
        choices=("bcsd", "china", "nam12"),
        default="bcsd",
        help="CF 数据源（默认：bcsd）",
    )
    parser.add_argument("--model", help="气候模型名；bcsd/china 必需")
    parser.add_argument(
        "--scenario",
        help="仅在只传一个 CSV 时允许；默认从 CSV 文件名推断且不传给科学入口",
    )
    parser.add_argument("--shp", default=DEFAULT_SHP, help="国家边界矢量文件")
    parser.add_argument("--cfs-dir", default=DEFAULT_CFS_DIR, help="CF 数据根目录")
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"科学结果目录（默认：{DEFAULT_OUTPUT_DIR}）",
    )
    parser.add_argument("--gcm", help="NAM-12 GCM 名")
    parser.add_argument("--realization", help="NAM-12 realization")
    parser.add_argument("--rcm", help="NAM-12 RCM 名")
    parser.add_argument("--region", help="BCSD 单一区域；默认由程序串行处理全部区域")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="把 --overwrite 传给科学入口；生产默认关闭",
    )
    parser.add_argument(
        "--max-dist",
        type=positive_float,
        default=DEFAULT_MAX_DIST,
        help=f"最近邻距离容差（默认：{DEFAULT_MAX_DIST}）",
    )
    parser.add_argument(
        "--years",
        type=int,
        nargs="+",
        default=None,
        metavar="YEAR",
        help="保留与科学入口一致的参数；生产作业禁止使用，以避免输出路径冲突",
    )

    # 作业生成参数。
    parser.add_argument(
        "--project-dir",
        default=str(repository_root),
        help="远程仓库目录（默认根据本脚本位置推断）",
    )
    parser.add_argument(
        "--jobs-dir",
        default=DEFAULT_JOBS_DIR,
        help=f"生成脚本目录（默认：{DEFAULT_JOBS_DIR}）",
    )
    parser.add_argument(
        "--logs-dir",
        default=DEFAULT_LOGS_DIR,
        help=f"Slurm 日志目录（默认：{DEFAULT_LOGS_DIR}）",
    )
    parser.add_argument(
        "--python-executable",
        default="python",
        help="激活 climate 环境后运行科学入口的 Python 命令（默认：python）",
    )
    parser.add_argument(
        "--conda-activate",
        default=DEFAULT_CONDA_ACTIVATE,
        help=f"conda 激活脚本（默认：{DEFAULT_CONDA_ACTIVATE}）",
    )
    parser.add_argument(
        "--conda-environment",
        default=DEFAULT_CONDA_ENVIRONMENT,
        help=f"conda 环境名（默认：{DEFAULT_CONDA_ENVIRONMENT}）",
    )
    parser.add_argument("--partition", default="wzhctest", help="Slurm 分区")
    parser.add_argument("--nodes", type=positive_int, default=1, help="节点数（默认：1）")
    parser.add_argument("--ntasks", type=positive_int, default=4, help="任务数（默认：4）")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印计划，不创建目录或脚本",
    )
    return parser


def scientific_identity(args: argparse.Namespace) -> str:
    if args.source == "nam12":
        return safe_token(args.gcm, "gcm")
    return safe_token(args.model, "model")


def validate_args(args: argparse.Namespace) -> None:
    if args.years:
        raise ValueError(
            "生产作业不允许 --years：科学入口的输出文件名不含年份，"
            "不同年份选择会发生路径冲突"
        )
    if args.source in {"bcsd", "china"} and not args.model:
        raise ValueError(f"{args.source} 数据源需要 --model")
    if args.source == "nam12":
        missing = [
            name
            for name in ("gcm", "realization", "rcm")
            if not getattr(args, name)
        ]
        if missing:
            raise ValueError("nam12 数据源缺少参数：" + ", ".join(f"--{x}" for x in missing))
        safe_token(args.realization, "realization")
        safe_token(args.rcm, "rcm")
    if args.scenario and len(args.csv) != 1:
        raise ValueError("--scenario 只能和单个 --csv 一起使用")
    safe_token(args.partition, "partition")
    scientific_identity(args)


def build_scientific_command(
    args: argparse.Namespace,
    *,
    csv_argument: str,
    explicit_scenario: Optional[str],
) -> Tuple[str, ...]:
    command = [
        args.python_executable,
        ENTRYPOINT,
        "--csv",
        csv_argument,
        "--source",
        args.source,
    ]
    if args.model:
        command.extend(("--model", args.model))
    if explicit_scenario:
        command.extend(("--scenario", explicit_scenario))
    if args.shp != DEFAULT_SHP:
        command.extend(("--shp", args.shp))
    if args.cfs_dir != DEFAULT_CFS_DIR:
        command.extend(("--cfs-dir", args.cfs_dir))
    if args.output_dir != DEFAULT_OUTPUT_DIR:
        command.extend(("--output-dir", args.output_dir))
    for name in ("gcm", "realization", "rcm", "region"):
        value = getattr(args, name)
        if value:
            command.extend((f"--{name}", value))
    if args.overwrite:
        command.append("--overwrite")
    if args.max_dist != DEFAULT_MAX_DIST:
        command.extend(("--max-dist", str(args.max_dist)))
    return tuple(command)


def create_plan(args: argparse.Namespace) -> Tuple[List[JobSpec], Path, Path, Path]:
    validate_args(args)
    project_dir = expanded_path(args.project_dir)
    jobs_dir = expanded_path(args.jobs_dir)
    logs_dir = expanded_path(args.logs_dir)
    conda_activate = expanded_path(args.conda_activate)

    if not project_dir.is_dir():
        raise ValueError(f"项目目录不存在：{project_dir}")
    if not (project_dir / ENTRYPOINT).is_file():
        raise ValueError(f"科学入口不存在：{project_dir / ENTRYPOINT}")

    csv_values = unique_values(args.csv, "csv")
    identity = scientific_identity(args)
    specs = []  # type: List[JobSpec]
    seen_scenarios = set()  # type: Set[str]
    seen_names = set()  # type: Set[str]
    for csv_argument in csv_values:
        csv_path = resolve_project_input(project_dir, csv_argument)
        if not csv_path.is_file():
            raise ValueError(f"场站 CSV 不存在：{csv_path}")
        scenario = args.scenario or infer_scenario(csv_argument)
        scenario = safe_token(scenario, "scenario")
        if scenario in seen_scenarios:
            raise ValueError(f"多个 CSV 推断为同一情景，会发生作业冲突：{scenario}")
        seen_scenarios.add(scenario)

        job_name = job_token(f"stout_{args.source}_{identity}_{scenario}")
        if job_name in seen_names:
            raise ValueError(f"作业名冲突：{job_name}")
        seen_names.add(job_name)
        script_path = jobs_dir / f"{job_name}.sh"
        command = build_scientific_command(
            args,
            csv_argument=csv_argument,
            explicit_scenario=args.scenario,
        )
        specs.append(JobSpec(csv_argument, scenario, job_name, script_path, command))
    return specs, project_dir, logs_dir, conda_activate


def render_job_script(
    args: argparse.Namespace,
    spec: JobSpec,
    *,
    project_dir: Path,
    logs_dir: Path,
    conda_activate: Path,
) -> str:
    q = shlex.quote
    lines = [
        "#!/usr/bin/env bash",
        f"#SBATCH --job-name={spec.job_name}",
        f"#SBATCH --partition={args.partition}",
        f"#SBATCH -N {args.nodes}",
        f"#SBATCH -n {args.ntasks}",
        f"#SBATCH --output={logs_dir}/{spec.job_name}_%j.out",
        f"#SBATCH --error={logs_dir}/{spec.job_name}_%j.err",
        "",
        "set -eo pipefail",
        f"source {q(str(conda_activate))} {q(args.conda_environment)}",
        "set -u",
        f"cd {q(str(project_dir))}",
        "",
        'echo "[INFO] 开始时间: $(date -Iseconds)"',
        'echo "[INFO] 节点: ${SLURMD_NODENAME:-$(hostname)}"',
        (
            f'echo "[INFO] unit={spec.job_name} source={args.source} '
            f'scenario={spec.scenario}"'
        ),
        shell_join(spec.command),
        f'echo "[STATION_OUTPUT_DONE] unit={spec.job_name}"',
        'echo "[INFO] 完成时间: $(date -Iseconds)"',
        "",
    ]
    return "\n".join(lines)


def write_script(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o755)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def create_jobs(args: argparse.Namespace) -> List[Path]:
    specs, project_dir, logs_dir, conda_activate = create_plan(args)
    collisions = [spec.script_path for spec in specs if spec.script_path.exists()]
    if collisions:
        formatted = "\n".join(f"  {path}" for path in collisions)
        raise FileExistsError(f"拒绝覆盖已有作业脚本：\n{formatted}")

    print(f"计划生成 {len(specs)} 个作业（source={args.source}）")
    for spec in specs:
        print(f"  {spec.job_name}: {shell_join(spec.command)}")
    if args.dry_run:
        print("dry-run：未创建目录或脚本")
        return []

    specs[0].script_path.parent.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    written = []  # type: List[Path]
    for spec in specs:
        content = render_job_script(
            args,
            spec,
            project_dir=project_dir,
            logs_dir=logs_dir,
            conda_activate=conda_activate,
        )
        write_script(spec.script_path, content)
        written.append(spec.script_path)
    print(f"已生成 {len(written)} 个作业脚本，未提交任何作业")
    return written


def main() -> None:
    configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args()
    try:
        create_jobs(args)
    except (FileExistsError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
