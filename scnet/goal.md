# SCNet 0.1° 场站出力工作流

## 1. 目标与边界

本工作流在 SCNet/Slurm 上使用 `station_output_calculator_0p1deg.py`，根据风光
容量因子计算逐时场站出力。`station_output_calculator_1deg.py` 已废弃，不进入
任何作业或完成判定。

完成一个任务单元必须同时满足：

1. 对应 Slurm 作业以 `COMPLETED` 和 `ExitCode=0:0` 结束；
2. `run_summary_*.csv` 中所有行都属于允许状态；
3. 汇总中 `ok` 或 `exists` 行对应的预期输出文件存在。

本工作流不修改科学算法，不在登录节点运行科学计算或批量 NetCDF 检查，不在
作业内部执行环境或输出检查，也不会未经授权提交、取消、重试或覆盖作业。

## 2. 工作负载契约

| 字段 | 契约 |
|---|---|
| 科学入口 | `python station_output_calculator_0p1deg.py --csv <CSV> --source <SOURCE> ...` |
| 默认数据源 | `bcsd` |
| 并行轴 | SSP；不同模式可以作为不同 campaign 独立运行 |
| 作业单位 | 一个 `source × model/GCM identity × SSP` |
| 作业内共享轴 | 程序按既有顺序串行处理该 SSP 的全部可见国家，以及 solar、wind |
| 依赖 | 对应 SSP 的 stations CSV、CF 目录、Natural Earth shapefile |
| DAG | 无；三个 SSP 作业相互独立 |
| 输出 | 默认位于 `<PROJECT_DIR>/outputs/outputs_0p1deg/` |
| 重启 | 默认不传 `--overwrite`，已有文件由科学程序跳过 |
| 资源 | 单节点、4 tasks；程序本身是单进程，多 tasks 主要用于取得按核分配的内存 |
| 时间限制 | 不写 `#SBATCH --time` |

默认一次生成以下三个作业：

```text
stations_SSP1-2.6.csv → ssp126
stations_SSP2-4.5.csv → ssp245
stations_SSP5-6.0.csv → ssp585
```

生成的 BCSD 科学命令等价于：

```bash
python station_output_calculator_0p1deg.py \
  --csv data/stations/stations_SSP1-2.6.csv \
  --source bcsd --model '<MODEL>'
```

`--scenario` 默认不传，由 CSV 文件名推断。生产作业禁止传 `--years`，因为科学
程序的输出文件名不包含年份，不同年份选择会写入相同路径。

## 3. 区域范围与现有数据

BCSD 程序以 `data/cfs/CFs_of_solar/<MODEL>/` 当前存在的区域目录作为运行范围，
不会使用固化的全球区域清单。某个服务器只有部分区域 CF 时，只处理当前可见
区域；尚在做 BCSD 降尺度或 CF 计算、尚未进入该目录的区域不算缺失。

例如当前 `scnet-wuzhen-199` 的 CANESM5 仅有 23 个可用 CF 区域，India、
Australia、Brazil 尚未进入 CF 计算阶段。本轮只处理已有区域，不等待也不为这
三个区域创建额外作业。

## 4. 本地实现和状态目录

- 作业生成器：`scnet/create_station_output_jobs.py`
- 只读监控器：`scnet/monitor_station_output_jobs.py`
- 本地运行状态：`scnet/models/<MODEL>/completion_status/`

`completion_status/` 是不纳入 Git 的运行期状态。监控器原子写入：

```text
completion_status/
├── progress.md
├── <SERVER>/<UNIT_ID>.json
└── latest_<SERVER>.json
```

监控器默认每 1800 秒检查一次，服务器通过 `--server` 在运行时指定，不写死在
代码中。每次检查后都会原子更新统一的 `progress.md` 表，即使状态没有变化。
它只采集、分类和报告；失败后的判断和操作由 Agent（Codex CLI 或 Claude Code）
完成。

## 5. 本地与远程边界

### 本地

- 修改和测试代码；
- commit/push；
- 运行定时监控器并保存状态；
- 根据监控证据决定后续操作。

### 远程登录节点

- HTTPS clone 或干净工作区的 fast-forward-only pull；
- 建立数据软链；
- 使用系统 `python3` 生成作业；
- `bash -n` 检查脚本；
- 查询 `squeue`、`sacct` 和少量日志/汇总文本；
- 在已激活的环境中做轻量 import 检查。

### 远程计算节点

- 运行 `station_output_calculator_0p1deg.py`；
- 所有科学计算、批量 NetCDF 读取和大内存校验。

远程默认路径：

```text
仓库：~/calculate_wind_solar_out
作业：~/jobs/station_output_0p1deg/
日志：~/logs/station_output_0p1deg/
结果：~/calculate_wind_solar_out/outputs/outputs_0p1deg/
```

作业和日志目录可以通过生成器参数覆盖。不得删除或覆盖远程的 `~/data`、已有
输出、日志、作业脚本或冲突的远程 checkout。

## 6. 环境与非 Git 数据

Slurm 作业必须使用：

```bash
source /work/home/acbpgywfpz/miniconda3/bin/activate climate
```

生成器本身仅依赖 Python 标准库，在登录节点使用系统 `python3` 即可。科学入口
的直接依赖已记录在 `requirements.txt`；远程安装到当前用户 HOME 后必须在上述
环境激活状态下验证 `numpy`、`pandas`、`netCDF4`、`shapely`、`shapefile`、
`tqdm` 和 `scipy` 可导入。

服务器数据适配层为：

```text
<PROJECT_DIR>/data/cfs      -> ~/data/cfs
<PROJECT_DIR>/data/maps     -> ~/data/maps
<PROJECT_DIR>/data/stations -> ~/data/stations
```

只在目标不存在时建立软链；如果目标已是文件、目录或不同软链，停止并人工检查。
非 `scnet-wuzhen-199` 服务器只有在共享文件系统可以访问源绝对路径时，才允许把
乌镇 199 的数据软链到该服务器的 `~/data/`，否则需要采用明确、非破坏式的数据
同步方案。

## 7. Slurm 规则

`scnet-wuzhen-*` 使用：

```bash
#SBATCH --partition=wzhctest
#SBATCH -N 1
#SBATCH -n 4
```

不设置 walltime。同一账号在同一台超算上的所有项目合计最多 20 个活动作业，
统计必须包含所有 pending、running、configuring、completing 等非终态作业，不能
按项目名过滤。提交与 BCSD 共用：

```text
$HOME/.bcsd_submit.lock
```

本 campaign 只有三个作业，不需要自动补槽。

## 8. 生成和本地验证

默认 BCSD 示例：

```bash
python3 scnet/create_station_output_jobs.py --model '<MODEL>' --dry-run
python3 scnet/create_station_output_jobs.py --model '<MODEL>'
```

生成器一次创建三个 `.sh`，不会调用 `sbatch`。如需 China 或 NAM-12：

```bash
python3 scnet/create_station_output_jobs.py \
  --source china --model '<MODEL>'

python3 scnet/create_station_output_jobs.py \
  --source nam12 --gcm '<GCM>' --realization '<REALIZATION>' --rcm '<RCM>'
```

每次生成后检查：

```bash
find ~/jobs/station_output_0p1deg -maxdepth 1 -name '*.sh' -print -exec bash -n {} \;
```

生成器拒绝覆盖同名脚本；若目录中已有脚本，应先确认它属于哪个 campaign，而
不是直接删除或覆盖。

## 9. 手动提交协议

只有获得明确提交授权后才能执行。持锁期间对账号全部活动作业重新计数，并逐个
填充可用槽位：

```bash
exec 9>"$HOME/.bcsd_submit.lock"
flock -w 60 9

for script in "$HOME"/jobs/station_output_0p1deg/*.sh; do
    active=$(squeue -h -u "$USER" | wc -l)
    if [ "$active" -ge 20 ]; then
        break
    fi
    sbatch "$script"
done
```

每次 `sbatch` 前都要重新计数。不得因为只有三个本项目作业，就忽略同账号其他
项目占用的槽位。

## 10. 监控、判断和后续操作

一次检查：

```bash
python3 scnet/monitor_station_output_jobs.py \
  --server '<SSH_HOST>' --model '<MODEL>' --once
```

每半小时持续检查：

```bash
python3 scnet/monitor_station_output_jobs.py \
  --server '<SSH_HOST>' --model '<MODEL>' --interval 1800
```

完成状态允许：

```text
ok、exists、no_stations
```

`ok` 和 `exists` 必须同时有对应输出文件。下列状态视为确定性数据/配置错误：

```text
no_cf、no_shape、未知状态
```

监控分类：

| 分类 | 含义 | 后续 |
|---|---|---|
| `not_submitted` | 未发现活动作业或近期 accounting 记录 | 报告，由 Agent 判断是否提交 |
| `active` | Slurm 非终态 | 等待，不重复提交 |
| `succeeded` | Slurm、汇总和输出证据均满足 | 终态成功 |
| `retryable` | TIMEOUT、NODE_FAIL、PREEMPTED 等 | 仅报告，由 Agent 查看日志后决定 |
| `resource_failure` | OUT_OF_MEMORY | 仅报告，根据 MaxRSS/资源证据决定 |
| `deterministic_failure` | 参数、代码、数据或其他明确失败 | 停止该单元，修复后再运行 |
| `incomplete_output` | Slurm 成功但完成契约不满足 | 不认定成功，交由 Agent 检查 |

监控器不会自动重试。异常作业可能留下被科学程序误判为“已存在”的部分 NetCDF；
在没有精确证据和明确授权时，不使用 `--overwrite`，不删除或移动输出。Agent 应
根据 Slurm 状态、退出码、日志尾部、汇总行和缺失输出路径决定精确恢复方式。

## 11. Git 与部署流程

1. 本地修改和测试。
2. 本地 commit 并 push。
3. 远程使用 HTTPS clone；已有 checkout 必须先确认干净，再 `git pull --ff-only`。
4. 检查数据源并建立三个子目录软链。
5. 安装/验证当前用户依赖。
6. 用系统 `python3` 生成脚本并执行 `bash -n`。
7. 未获提交授权时停在 `sbatch` 前。

远程仓库若脏、分支错误、已分叉、目标路径冲突、匿名 HTTPS 无权访问或数据软链
不可读，必须停止并报告，禁止 `git reset --hard`、覆盖或私自更换认证方式。
