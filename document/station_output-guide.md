# 场站出力计算指南

本文档说明如何基于场站选址结果（`stations_SSP*.csv`）和风光容量因子数据，计算各场站的逐时出力，并输出为 NetCDF 文件。

---

## 1. 输入数据：场站选址结果

### 1.1 文件列表

| 文件 | 情景 | CF 情景代码 |
|------|------|------------|
| `data/stations/stations_SSP1-2.6.csv` | SSP1-2.6 | ssp126 |
| `data/stations/stations_SSP2-4.5.csv` | SSP2-4.5 | ssp245 |
| `data/stations/stations_SSP5-6.0.csv` | SSP5-6.0 | ssp585 |

> **注意**：CSV 文件命名为 `SSP5-6.0`，对应的 CF 文件中情景代码为 `ssp585`。

### 1.2 CSV 格式

```csv
year,type,lon,lat,capacity_gw
2050,solar,-126.5,54.5,5.7757
2050,solar,-126.5,53.5,3.4574
2050,wind,25.5,62.5,0.1901
2040,solar,12.5,42.5,3.2100
2030,wind,-120.5,51.5,0.8502
```

**每一行代表一个选中的 1°×1° 格网单元（即一个"场站"）。**

各列含义：

| 列名 | 类型 | 说明 |
|------|------|------|
| `year` | int | 目标年份：2030、2040、2050 |
| `type` | string | 场站类型：`solar`（光伏）或 `wind`（风电） |
| `lon` | float | 格网中心经度（°），范围 **[-179.5, 179.5]**，步长 1° |
| `lat` | float | 格网中心纬度（°），范围 **[-89.5, 89.5]**，步长 1° |
| `capacity_gw` | float | 该格网的最大可装机容量（GW，铭牌值） |

### 1.3 经纬度含义

- `(lon, lat)` 是 **1°×1° 全球规则格网的中心点坐标**
- 格网编号体系：180 行（纬度）× 360 列（经度），共 64,800 个格元
- 格元 `(lon, lat)` 覆盖的空间范围是 `[lon-0.5, lon+0.5] × [lat-0.5, lat+0.5]`
- 例如 `lon=-126.5, lat=54.5` 表示覆盖 `[-127.0, -126.0] × [54.0, 55.0]` 的 1°×1° 区域
- 经度范围：-179.5 到 179.5（非 -180 到 180），纬度范围：-89.5 到 89.5
- 场站选址是三阶段嵌套优化的结果（2050→2040→2030），更近年份的场站是更远年份的子集

### 1.4 场站去重

由于场站选址是三阶段嵌套优化，**同一格网点 `(lon, lat, type)` 可能出现在多个年份行中**。例如：

```csv
2050,wind,-7.5,52.5,1.7371
2040,wind,-7.5,52.5,1.7371
2030,wind,-7.5,52.5,1.7371
```

处理方式：
1. 按 `(lon, lat, type)` **去重**，保留最早年份作为 **激活年份**（`activation_year`）
2. 去重后每个格网点只有一个场站记录
3. 出力仅在 CF 年份 ≥ `activation_year` 时有效，之前为 NaN

### 1.5 `capacity_gw` 计算

容量由该格网的可用面积和安装密度决定：

- **光伏**：`capacity = 74 (MW/km²) × 可用面积比例 × 格网面积 (km²) / 1000`
- **风电陆上**：`capacity = 2.7 (MW/km²) × 可用面积比例 × 格网面积 (km²) / 1000`
- **风电海上**：`capacity = 4.6 (MW/km²) × 可用面积比例 × 格网面积 (km²) / 1000`

~~这是格网内所有可开发土地的装机容量之和（铭牌最大值），不是实际装机量。~~

---

## 2. 输入数据：风光容量因子

容量因子数据由 `calculate_bcsd_cfs` 模块生成，涵盖三种数据源。

### 2.1 数据来源

| 数据源 | 气候数据 | 覆盖范围 |
|--------|---------|---------|
| BCSD | CMIP6-ERA5Land 降尺度（MIROC-ES2H、MPI-ESM1-2-HR、NESM3） | 全球多个国家/地区 |
| China | CMIP6-CMFD 中国区域降尺度（MIROC-ES2H、NESM3、CanESM5） | 中国全域 |
| NAM-12 | CORDEX 北美区域模式 | 北美（加拿大、美国、墨西哥） |

> **注意**：
> - BCSD 数据源仅包含 MIROC-ES2H、MPI-ESM1-2-HR、NESM3 三个模型
> - China 数据源包含 MIROC-ES2H、NESM3、**CanESM5**（CanESM5 仅存在于 China 数据源，无对应的 BCSD 数据）
> - NAM-12 数据暂未生成

**排放情景**：

| 情景 | 代码 | 说明 |
|------|------|------|
| SSP1-2.6 | ssp126 | 低排放 |
| SSP2-4.5 | ssp245 | 中等排放 |
| SSP5-8.5 | ssp585 | 高排放 |

> **注意**：并非所有 模型×区域×情景 组合都存在。例如 NESM3/Austria/solar 仅有 ssp126 和 ssp245，缺少 ssp585。

### 2.2 文件路径与命名规则

文件根目录：`data/cfs`

**BCSD 容量因子**（有 `{region}` 子目录）：
```
data/cfs/CFs_of_solar/{model}/{region}/solar_CF_{region}_{model}_{scenario}_{years}_{months}.nc
data/cfs/CFs_of_wind/{model}/{region}/wind_CF_{region}_{model}_{scenario}_{years}_{months}.nc
```
示例：`solar_CF_Germany_MIROC-ES2H_ssp126_2015-2060_allmonths.nc`

**China 容量因子**（**无** `{region}` 子目录，文件直接在 `{model}/` 下）：
```
data/cfs/CFs_of_solar_china/{model}/solar_CF_china_{model}_{scenario}_{years}_{months}.nc
data/cfs/CFs_of_wind_china/{model}/wind_CF_china_{model}_{scenario}_{years}_{months}.nc
```
示例：`solar_CF_china_MIROC-ES2H_ssp126_2015-2060_allmonths.nc`

> **注意**：China 的 MIROC-ES2H 目录下同时存在 `2015-2060` 和 `2060-2060` 两种年份范围的文件，应选择年份范围最大的（`2015-2060`）。其他模型（NESM3、CanESM5）只有 `2015-2060` 文件。

**NAM-12 容量因子**：
```
data/cfs/CFs_of_solar_NAM-12/{gcm}/{realization}/solar_CF_NAM-12_{gcm}_{realization}_{rcm}_{scenario}_{years}_{months}.nc
data/cfs/CFs_of_wind_NAM-12/{gcm}/{realization}/wind_CF_NAM-12_{gcm}_{realization}_{rcm}_{scenario}_{years}_{months}.nc
```
示例：`solar_CF_NAM-12_MPI-ESM1-2-LR_r1i1p1f1_CRCM5_ssp126_2020-2060_allmonths.nc`

其中 `{months}` 为 `allmonths`（全年 1-12 月）或 `m01-02-07-08`（指定月份，零填充、短横分隔）。

### 2.3 数据规格

| 属性 | BCSD | China | NAM-12 |
|------|------|-------|--------|
| 文件格式 | NetCDF4 | NetCDF4 | NetCDF4 |
| 时间范围 | 2015–2060 | 2015–2060（部分模型到 2100） | 2020–2060（因模式而异） |
| 时间分辨率 | **3 小时**（8 步/天） | **3 小时**（8 步/天） | **1 小时**（24 步/天） |
| 空间分辨率 | 0.1° × 0.1°（规则） | **不规则**（见下方说明） | ~0.11°（旋转极网格） |
| CF 单位 | 无量纲 [0, 1] | 无量纲 [0, 1] | 无量纲 [0, 1] |
| 存储类型 | float32, zlib | float32, zlib | float32, zlib |
| 坐标系 | 规则经纬度 (`lat`, `lon`) | 规则经纬度 (`lat`, `lon`) | **旋转极** (`rlat`, `rlon`，含 2D `lat`/`lon`) |
| **经度范围** | **0°–360°** | **0°–360°** | 待确认 |

**BCSD 和 China 的网格是不一样的**
- BCSD 是 ERA5Land 网格（规则 0.1° 步长）
- China 是 CMFD 网格（**不规则步长**）

**China 网格不规则说明**：
- 纬度步长为 0.1° 或 0.2°（非固定值）
- 经度步长为 0.1° ~ 0.4°（非固定值）
- 不能假设规则网格步长，空间索引必须使用实际坐标值
- 范围：经度 70.05°–139.95°，纬度 15.05°–54.95°

**经度约定（关键）**：

BCSD 和 China 的 CF 文件经度均使用 **0°–360°** 范围，而场站 CSV 使用 **-180°–180°**。需要在计算时进行转换。

部分区域经度示例：

| 区域 | CF 经度范围（0-360°） | 说明 |
|------|----------------------|------|
| Germany | 5.0 – 15.0 | 正常 |
| Ireland | 349.5 – 353.9 | 跨越 360°（即 -10.5° ~ -6.1°） |
| Spain | 0.1 – 360.0 | **跨越全球**（包含本初子午线两侧） |
| Portugal | 328.7 – 353.7 | 跨越 360° |
| Chile | 250.6 – 293.6 | 西半球 |
| México | 241.7 – 273.3 | 西半球 |
| China | 70.05 – 139.95 | 正常 |

> **处理经度跨越 0°/360° 的情况**：当场站 bbox 的 `[lon-0.5, lon+0.5)` 跨越 0°/360° 边界时（如 lon=0.5° 或 lon=359.5° 的场站），需要分段匹配 CF 格点。

### 2.4 变量说明

**BCSD / China 容量因子**：

| 变量名 | 维度 | 类型 | 说明 |
|--------|------|------|------|
| `solar_cf` / `wind_cf` | `(time, lat, lon)` | float32 | 容量因子，值域 [0, 1] |
| `time` | `(time)` | float64 | 时间戳（`units: days since 1850-01-01`） |
| `lat` | `(lat)` | float32 | 纬度（°） |
| `lon` | `(lon)` | float32 | 经度（°，**0–360 范围**） |

> **注意**：wind CF 的时间步数可能比 solar CF 少 1 步（如 Germany：solar=134416，wind=134415）。

**NAM-12 容量因子**（旋转极网格）：

| 变量名 | 维度 | 类型 | 说明 |
|--------|------|------|------|
| `solar_cf` / `wind_cf` | `(time, rlat, rlon)` | float32 | 容量因子，值域 [0, 1] |
| `time` | `(time)` | datetime64 | 时间戳 |
| `rlat` | `(rlat)` | float32 | 旋转纬度 |
| `rlon` | `(rlon)` | float32 | 旋转经度 |
| `lat` | `(rlat, rlon)` | float32 | 地理纬度（2D 辅助坐标） |
| `lon` | `(rlat, rlon)` | float32 | 地理经度（2D 辅助坐标） |

### 2.5 年份与场站匹配策略

容量因子数据覆盖 **2015–2060** 年，但场站选址仅有 **2030、2040、2050** 三个年份。由于场站选址是三阶段嵌套优化（2050 → 2040 → 2030），更近年份的场站是更远年份的子集（即 2030 场站 ⊂ 2040 场站 ⊂ 2050 场站）。

**实现方式**：通过去重后的 `activation_year` 字段实现年份掩码：

1. 将所有场站按 `(lon, lat, type)` 去重，记录最早出现年份为 `activation_year`
2. 对于每个场站，出力仅在 CF 年份 ≥ `activation_year` 时有效
3. CF 年份 < `activation_year` 时，该场站出力为 NaN

| 容量因子年份 | 场站状态 |
|:---:|------|
| 2015–2029 | 所有场站出力为 NaN（场站尚未激活） |
| 2030–2039 | 仅 activation_year ≤ 2030 的场站有出力 |
| 2040–2049 | 仅 activation_year ≤ 2040 的场站有出力 |
| 2050–2060 | 所有场站有出力 |

---

## 3. 输入数据：国家边界矢量

### 3.1 数据文件

| 文件 | 说明 |
|------|------|
| `data/maps/natural_earth/ne_110m_admin_0_countries.shp` | Natural Earth 110m 全球国家边界矢量 |

### 3.2 用途

容量因子数据的网格范围是各国外接矩形（bounding box），包含国界外的格点。例如 Germany 的 CF 文件覆盖 `[5°E, 15°E] × [47°N, 55°N]`，其中包含非德国领土的格点。因此需要国家边界矢量来：

1. **筛选场站**：确定每个场站格元（1°×1°）是否属于该国家
2. **筛选 CF 格点**：在聚合时，只取国家边界内的 CF 格点参与计算

### 3.3 边界格点处理

场站格元为 1°×1°，往往横跨国界。处理原则：

- **格点中心在国家边界内** → 该场站归属该国，参与出力计算
- **格点中心在国家边界外** → 该场站不归属该国，跳过
- 聚合 CF 时，在 `[lon-0.5, lon+0.5] × [lat-0.5, lat+0.5]` 范围内，**只取落入国家边界内部的** 0.1° CF 格点参与空间均值计算
- 若某场站格元内所有 CF 格点均在国界外，该场站标记为 NaN

### 3.4 BCSD 区域名与 Natural Earth 国家名映射

BCSD CF 文件的区域目录名与 Natural Earth shapefile 中的 `NAME` 字段存在差异，需要映射：

| BCSD 目录名 | Natural Earth NAME | 说明 |
|-------------|-------------------|------|
| `South-Africa` | `South Africa` | 连字符 vs 空格 |
| `South-Korea` | `South Korea` | 连字符 vs 空格 |
| `United-Kingdom` | `United Kingdom` | 连字符 vs 空格 |
| `México` | `Mexico` | 重音符号 |
| 其他（如 Germany, Spain 等） | 相同 | 无需映射 |

---

## 4. 网格不匹配与聚合方案

### 4.1 问题

| 数据 | 空间分辨率 | 网格数量 |
|------|-----------|---------|
| 场站选址（CSV） | 1°×1° | 180×360 = 64,800 |
| 容量因子（BCSD） | 0.1°×0.1°（规则） | ~1800×3600 |
| 容量因子（China） | 不规则（~0.1°–0.4°） | 348×442 |

一个场站格元（1°×1°）内包含约 **~100 个** 0.1° CF 格元。容量因子数据更精细，需要聚合到场站格元尺度。

### 4.2 聚合方法

对属于该国家的场站格元 `(lon, lat)`，在其覆盖范围 `[lon-0.5, lon+0.5) × [lat-0.5, lat+0.5)` 内：

1. **筛选 CF 格点**：只保留落入国家边界多边形内部的 CF 格点（参见第 3 节）
2. **取空间均值**：

```
CF_station(lon, lat, t) = mean( CF_within_country(t) )
```

光伏和风电均取空间均值。

### 4.3 边界处理

- 格元边界使用左闭右开 `[lon-0.5, lon+0.5)`，避免重叠
- 经度 -180°/180° 边界需特殊处理（环形衔接）
- 国界外的 CF 格点不参与均值计算
- 无数据区域（海洋中的陆上风电格元等）跳过，取有效格元均值

---

## 5. 出力计算

聚合得到容量因子 `CF(t)` 后，场站出力为：

```
power(t) = CF(t) × capacity_gw    # 单位：GW
```

- 出力仅在 CF 年份 ≥ `activation_year` 时有效，否则为 NaN
- 若场站格元内所有 CF 格点均为 NaN（如海洋中的陆上风电），出力也为 NaN

---

## 6. 输出格式：NetCDF

### 6.1 文件结构

保存根目录为 `outputs`，按 **光伏/风电** 和 **数据源** 分开存放。

**BCSD / China 场站出力**（China 的 `{region}` 为 `china`）：
```
outputs/pv_out/{model}/{region}/pv_stations_out_{region}_{model}_{scenario}_{years}_{months}.nc
outputs/wind_out/{model}/{region}/wind_stations_out_{region}_{model}_{scenario}_{years}_{months}.nc
```
示例：
```
outputs/pv_out/MIROC-ES2H/Germany/pv_stations_out_Germany_MIROC-ES2H_ssp126_2015-2060_allmonths.nc
outputs/wind_out/MIROC-ES2H/Spain/wind_stations_out_Spain_MIROC-ES2H_ssp245_2015-2060_allmonths.nc
outputs/pv_out/MIROC-ES2H/china/pv_stations_out_china_MIROC-ES2H_ssp126_2015-2060_allmonths.nc
outputs/wind_out/NESM3/china/wind_stations_out_china_NESM3_ssp585_2015-2060_allmonths.nc
```

**NAM-12 场站出力**（单独路径，包含 GCM、realization、RCM 信息）：
```
outputs/pv_out_NAM-12/{gcm}/{realization}/pv_stations_out_NAM-12_{gcm}_{realization}_{rcm}_{scenario}_{years}_{months}.nc
outputs/wind_out_NAM-12/{gcm}/{realization}/wind_stations_out_NAM-12_{gcm}_{realization}_{rcm}_{scenario}_{years}_{months}.nc
```
示例：
```
outputs/pv_out_NAM-12/MPI-ESM1-2-LR/r1i1p1f1/pv_stations_out_NAM-12_MPI-ESM1-2-LR_r1i1p1f1_CRCM5_ssp126_2020-2060_allmonths.nc
outputs/wind_out_NAM-12/MPI-ESM1-2-LR/r1i1p1f1/wind_stations_out_NAM-12_MPI-ESM1-2-LR_r1i1p1f1_CRCM5_ssp126_2020-2060_allmonths.nc
```

### 6.2 维度

| 维度 | 说明 | 示例大小 |
|------|------|---------|
| `time` | 时间戳 | BCSD/China: ~134,416（46 年 × 8 步/天）；NAM-12: ~351,360（40 年 × 24 步/天） |
| `id` | 场站编号（0-based） | 取决于国家大小和类型 |

### 6.3 变量

| 变量 | 维度 | 类型 | 单位 | 说明 |
|------|------|------|------|------|
| `power` | `(time, id)` | float32 | GW | 场站出力 |
| `time` | `(time)` | float64 | — | 时间戳（`days since 1850-01-01`） |
| `station_lon` | `(id)` | float32 | ° | 场站经度 |
| `station_lat` | `(id)` | float32 | ° | 场站纬度 |
| `station_type` | `(id)` | int8 | — | 0=光伏, 1=风电 |
| `capacity_gw` | `(id)` | float32 | GW | 装机容量（铭牌） |
| `activation_year` | `(id)` | int32 | year | 场站激活年份（出力从此年开始有效） |

### 6.4 全局属性

```
:region = "Germany"
:scenario = "ssp126"
:source_csv = "stations_SSP1-2.6.csv"
```

NAM-12 额外属性：
```
:gcm = "MPI-ESM1-2-LR"
:realization = "r1i1p1f1"
:rcm = "CRCM5"
```

---

## 7. 处理流程汇总

```
stations_SSP*.csv       风光容量因子 (BCSD/China/NAM-12)    ne_110m_admin_0_countries.shp
        │                       │                                    │
        │  按国家筛选场站         │  solar_cf / wind_cf                │  国家边界多边形
        │  按 (lon,lat,type)     │  (time, lat/rlat, lon/rlon)        │
        │   去重+记录激活年份     │                                    │
        │  → 该国场站列表         │                                    │
        │                       │                                    │
        ▼                       ▼                                    ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  对该国每个场站 (lon, lat):                                          │
   │  1. 确定其 1°×1° 覆盖范围 bbox                                       │
   │  2. 找到 bbox 内所有 CF 格元                                          │
   │  3. 用国家边界筛选：只保留国界内的 CF 格点                              │
   │  4. 取筛选后格元 CF 的空间均值 → CF_station                            │
   │  5. 应用年份掩码：CF 年份 < activation_year 时 CF_station = NaN       │
   │  6. power = CF_station × capacity_gw                                │
   └─────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
                  outputs/pv_out/{model}/{region}/pv_stations_out_{region}_{model}_{scenario}_{years}_{months}.nc
                  outputs/wind_out/{model}/{region}/wind_stations_out_{region}_{model}_{scenario}_{years}_{months}.nc
                  outputs/pv_out_NAM-12/{gcm}/{realization}/pv_stations_out_NAM-12_{gcm}_{realization}_{rcm}_{scenario}_{years}_{months}.nc
                  outputs/wind_out_NAM-12/{gcm}/{realization}/wind_stations_out_NAM-12_{gcm}_{realization}_{rcm}_{scenario}_{years}_{months}.nc
                  dims: time × id(N_stations_in_country)
```

---

## 8. 实现注意事项

### 8.1 经度转换

CF 文件使用 0–360° 经度，场站 CSV 使用 -180–180°，需在空间匹配前统一转换：
- 场站经度转 0-360：`lon_360 = lon % 360`
- CF 经度转 -180~180（用于国家边界 contains 检测）：`lon_180 = ((lon + 180) % 360) - 180`

### 8.2 CF 文件查找

- China 数据源文件直接在 `{model}/` 目录下，无 `{region}` 子目录
- 同一模型/情景下可能存在多个年份范围的文件（如 `2015-2060` 和 `2060-2060`），应选择年份范围最大的
- 部分 模型×区域×情景 组合可能缺失，需跳过并记录警告

### 8.3 BCSD 模型覆盖的国家列表

不同模型覆盖的国家数量不同：

| 模型 | 国家数量 | 备注 |
|------|---------|------|
| MIROC-ES2H | 23 | 基础集合 |
| MPI-ESM1-2-HR | 20 | 缺少 Chile, India, Japan, México |
| NESM3 | 26 | 包含 Australia, Brazil |

### 8.4 性能考虑

- BCSD/China CF 文件大小约 800MB–19GB（China），建议分块读取（chunk_size=1000 时间步）
- 国家边界内的 CF 格点筛选可预先计算并缓存索引
- 空间均值使用 `np.nanmean` 向量化计算，避免逐时间步 Python 循环

---

ps：使用 `source .venv/bin/activate` 激活 Python 虚拟环境。
