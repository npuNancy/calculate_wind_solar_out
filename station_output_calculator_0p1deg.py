#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
0.1° 场站出力计算模块

与 1° 版本（station_output_calculator.py）的区别：
- 场站为单个 0.1° 格元（不再是含 ~100 个 CF 子格点的 1°×1° 格元）
- 场站网格与 CF 网格尺度相同，采用 **最近邻取点（nearest-neighbor）** 配准，
  而非 bbox 空间聚合
- 自动检查场站网格与 CF 网格是否一致：
    * BCSD：规则 0.1° 整数偏移，与场站网格对齐（最近邻距离≈0）
    * China：偏移 0.05° + 纬向步长不规则，不对齐（最近邻距离≈0.05–0.11°）
    * NAM-12：旋转极 2D 网格，不对齐
- 通过 --max-dist 容差剔除落在 CF 覆盖外的场站（出力 NaN）

详见 document/station_output_0.1deg-plan.md。

用法示例:
# BCSD - 全部国家
python station_output_calculator_0p1deg.py \
    --csv data/stations/stations_SSP1-2.6.csv \
    --source bcsd --model MIROC-ES2H --scenario ssp126

# BCSD - 仅 Germany
python station_output_calculator_0p1deg.py \
    --csv data/stations/stations_SSP1-2.6.csv \
    --source bcsd --model MIROC-ES2H --scenario ssp126 --region Germany

# China
python station_output_calculator_0p1deg.py \
    --csv data/stations/stations_SSP2-4.5.csv \
    --source china --model MIROC-ES2H --scenario ssp245

# NAM-12
python station_output_calculator_0p1deg.py \
    --csv data/stations/stations_SSP5-6.0.csv \
    --source nam12 --gcm MPI-ESM1-2-LR --realization r1i1p1f1 --rcm CRCM5
"""

import argparse
import os
import re
import logging

import numpy as np
import pandas as pd
import netCDF4 as nc
from shapely.geometry import Point, shape
from shapely.prepared import prep
import shapefile
from tqdm import tqdm

try:
    from scipy.spatial import cKDTree
    _HAS_SCIPY = True
except ImportError:  # pragma: no cover
    _HAS_SCIPY = False

# ---------------------------------------------------------------------------
# 常量（与 1° 版本保持一致）
# ---------------------------------------------------------------------------

SSP_MAP = {
    "SSP1-2.6": "ssp126",
    "SSP2-4.5": "ssp245",
    "SSP5-6.0": "ssp585",
    "SSP5-8.5": "ssp585",
}

CF_SUBDIR = {
    "bcsd": {"solar": "CFs_of_solar", "wind": "CFs_of_wind"},
    "china": {"solar": "CFs_of_solar_china", "wind": "CFs_of_wind_china"},
    "nam12": {"solar": "CFs_of_solar_NAM-12", "wind": "CFs_of_wind_NAM-12"},
}

CF_VARNAME = {"solar": "solar_cf", "wind": "wind_cf"}

BCSD_REGION_TO_NAME = {
    "South-Africa": "South Africa",
    "South-Korea": "South Korea",
    "United-Kingdom": "United Kingdom",
    "México": "Mexico",
}

# 默认最近邻匹配距离容差（度）。BCSD≈0，China/NAM-12≈0.05–0.11，0.15 足够覆盖。
DEFAULT_MAX_DIST = 0.15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def lon_to_360(lon):
    """经度 [-180,180] → [0,360]。"""
    return lon % 360


def lon_to_180(lon):
    """经度 [0,360] → [-180,180]。"""
    return ((lon + 180) % 360) - 180


def infer_scenario_from_csv(csv_path):
    basename = os.path.basename(csv_path)
    for ssp_name, ssp_code in SSP_MAP.items():
        if ssp_name in basename:
            return ssp_code
    raise ValueError(
        f"无法从文件名 '{basename}' 推断 SSP 情景，支持: {list(SSP_MAP.keys())}"
    )


def load_country_shapes(shp_path):
    sf = shapefile.Reader(shp_path)
    fields = [f[0] for f in sf.fields[1:]]
    name_idx = fields.index("NAME")
    countries = {}
    for i, rec in enumerate(sf.records()):
        name = rec[name_idx]
        geom = sf.shape(i).__geo_interface__
        countries[name] = shape(geom)
    return countries


def bcsd_region_to_ne_name(region_dir):
    return BCSD_REGION_TO_NAME.get(region_dir, region_dir)


def find_cf_file(cfs_dir, source, stype, model, region, scenario):
    """查找 BCSD / China CF 文件路径（与 1° 版本一致）。"""
    subdir = CF_SUBDIR[source][stype]
    if source == "china":
        base = os.path.join(cfs_dir, subdir, model)
    else:
        base = os.path.join(cfs_dir, subdir, model, region)

    if not os.path.isdir(base):
        return None

    candidates = [f for f in os.listdir(base)
                  if f.endswith(".nc") and scenario in f and "allmonths" in f]
    if not candidates:
        candidates = [f for f in os.listdir(base)
                      if f.endswith(".nc") and scenario in f]
    if not candidates:
        return None

    def year_span(fname):
        m = re.search(r'_(\d{4})-(\d{4})_', fname)
        return int(m.group(2)) - int(m.group(1)) if m else 0

    candidates.sort(key=year_span, reverse=True)
    return os.path.join(base, candidates[0])


def find_cf_files_nam12(cfs_dir, stype, gcm, realization, rcm, scenario, years=None):
    """查找 NAM-12 CF 文件（按年分文件）。

    目录结构::

        {cfs_dir}/CFs_of_{stype}_NAM-12/{gcm}/{realization}/{scenario}/yearly/
            {stype}_CF_NAM-12_{gcm}_{realization}_{rcm}_{scenario}_{year}_allmonths.nc

    每个文件含一个模式年（约 Dec(Y-1) → Dec(Y)）的逐小时 CF，因此完整日历年 Y
    跨文件 Y 与 Y+1。返回**按年份排序的文件路径列表**，找不到返回 ``[]``。

    指定 ``years`` 时仅返回覆盖这些日历年所需的文件（年 Y 需文件 Y 和 Y+1），
    以减少 IO；未指定则返回全部年份文件。
    """
    subdir = CF_SUBDIR["nam12"][stype]
    base = os.path.join(cfs_dir, subdir, gcm, realization, scenario, "yearly")
    if not os.path.isdir(base):
        return []

    year_pat = re.compile(r'_(\d{4})_allmonths\.nc$')
    found = {}
    for f in os.listdir(base):
        if not f.endswith(".nc") or scenario not in f or rcm not in f:
            continue
        m = year_pat.search(f)
        if not m:
            continue
        found[int(m.group(1))] = os.path.join(base, f)

    if not found:
        return []

    if years:
        wanted = set(years) | {y + 1 for y in years}
        sel_years = sorted(y for y in found if y in wanted)
    else:
        sel_years = sorted(found)

    return [found[y] for y in sel_years]


# ---------------------------------------------------------------------------
# 最近邻配准（核心）
# ---------------------------------------------------------------------------

def nearest_index_regular(cf_lons_360, cf_lats, station_lons_360, station_lats):
    """规则经纬网（1D lat / 1D lon）的最近邻索引。

    经纬可分离：分别在 lat、lon 一维坐标上求最近格点。
    lon 使用环形距离以正确处理 0°/360° 衔接。

    Parameters
    ----------
    cf_lons_360 : (n_lon,) CF 经度（0–360）
    cf_lats : (n_lat,) CF 纬度
    station_lons_360 : (n_sta,) 场站经度（0–360）
    station_lats : (n_sta,) 场站纬度

    Returns
    -------
    lat_idx : (n_sta,) 最近纬度索引
    lon_idx : (n_sta,) 最近经度索引
    dist : (n_sta,) 最近邻距离（度，max(|dlat|,|dlon_circular|)）
    """
    cf_lons_360 = np.asarray(cf_lons_360, dtype=np.float64)
    cf_lats = np.asarray(cf_lats, dtype=np.float64)
    n_sta = len(station_lons_360)

    lat_idx = np.empty(n_sta, dtype=np.int64)
    lon_idx = np.empty(n_sta, dtype=np.int64)
    dlat = np.empty(n_sta, dtype=np.float64)
    dlon = np.empty(n_sta, dtype=np.float64)

    for i in range(n_sta):
        # 纬度：直接 argmin（坐标量级 ~几百，开销可忽略）
        d_lat = np.abs(cf_lats - station_lats[i])
        j_lat = np.argmin(d_lat)
        lat_idx[i] = j_lat
        dlat[i] = d_lat[j_lat]

        # 经度：环形距离
        d_lon = np.abs(((cf_lons_360 - station_lons_360[i] + 180.0) % 360.0) - 180.0)
        j_lon = np.argmin(d_lon)
        lon_idx[i] = j_lon
        dlon[i] = d_lon[j_lon]

    dist = np.maximum(dlat, dlon)
    return lat_idx, lon_idx, dist


def nearest_index_2d(lat2d, lon2d_180, station_lons_180, station_lats):
    """旋转极 2D 网格的最近邻（NAM-12）。

    用地理坐标 (lat, lon) 在平面上做最近邻（北美无经度环绕问题）。

    Returns
    -------
    rlat_idx, rlon_idx : (n_sta,) 在 (rlat, rlon) 网格中的索引
    dist : (n_sta,) 最近邻平面距离（度）
    """
    n_rlat, n_rlon = lat2d.shape
    lat_flat = lat2d.ravel()
    lon_flat = lon2d_180.ravel()
    pts = np.column_stack([lon_flat, lat_flat])
    queries = np.column_stack([station_lons_180, station_lats])

    if _HAS_SCIPY:
        tree = cKDTree(pts)
        dist, flat_idx = tree.query(queries, k=1)
    else:
        flat_idx = np.empty(len(queries), dtype=np.int64)
        dist = np.empty(len(queries), dtype=np.float64)
        for i, q in enumerate(queries):
            d = (lon_flat - q[0]) ** 2 + (lat_flat - q[1]) ** 2
            j = np.argmin(d)
            flat_idx[i] = j
            dist[i] = np.sqrt(d[j])

    rlat_idx = flat_idx // n_rlon
    rlon_idx = flat_idx % n_rlon
    return rlat_idx, rlon_idx, dist


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class StationOutputCalculator0p1:
    """0.1° 场站出力计算器（最近邻取点）。"""

    def __init__(self, csv_path, source, model, scenario, shp_path,
                 cfs_dir, output_dir, gcm=None, realization=None, rcm=None,
                 region=None, overwrite=False, max_dist=DEFAULT_MAX_DIST,
                 years=None):
        self.csv_path = csv_path
        self.source = source
        self.model = model
        self.scenario = scenario
        self.shp_path = shp_path
        self.cfs_dir = cfs_dir
        self.output_dir = output_dir
        self.gcm = gcm
        self.realization = realization
        self.rcm = rcm
        self.region = region
        self.overwrite = overwrite
        self.max_dist = max_dist
        # 仅计算指定日历年份（如 [2030, 2040, 2050]）；None 表示全部年份
        self.years = sorted(set(years)) if years else None

        # 运行汇总：每条 (region, stype) 的处理结果
        self.summary = []

        self.stations_df = pd.read_csv(csv_path)
        logger.info(
            f"加载场站数据: {len(self.stations_df)} 条记录, 情景={scenario}, "
            f"最近邻容差={max_dist}°"
        )

        if self.years:
            logger.info(f"仅计算指定年份的发电量: {self.years}")

        self.country_shapes = load_country_shapes(shp_path)
        logger.info(f"加载国家边界: {len(self.country_shapes)} 个国家/地区")

    # ------------------------------------------------------------------

    def _select_time_indices(self, cf_years):
        """返回落在 self.years 内的时间步索引；未指定年份则返回 None（全部）。"""
        if not self.years:
            return None
        sel = np.where(np.isin(cf_years, np.array(self.years, dtype=cf_years.dtype)))[0]
        return sel

    # ------------------------------------------------------------------

    # 处理结果状态码 → 中文说明
    STATUS_DESC = {
        "ok": "已生成",
        "no_stations": "跳过：该区域无此类型场站",
        "no_cf": "跳过：未找到 CF 文件",
        "exists": "跳过：输出文件已存在",
        "no_shape": "跳过：shapefile 中无对应边界",
    }

    def _record(self, region, stype, status, n_stations=0):
        """记录一条 (region, stype) 的处理结果用于运行汇总。"""
        self.summary.append({
            "region": region,
            "type": stype,
            "scenario": self.scenario,
            "model": self.model or self.gcm or "",
            "status": status,
            "n_stations": int(n_stations),
        })

    def run(self):
        if self.source == "china":
            self._run_china()
        elif self.source == "nam12":
            self._run_nam12()
        else:
            self._run_bcsd()
        self._report_summary()

    # ------------------------------------------------------------------
    # 运行汇总报告
    # ------------------------------------------------------------------

    def _report_summary(self):
        """打印运行汇总并写出 CSV 报告。"""
        if not self.summary:
            logger.info("运行汇总：无任何处理记录。")
            return

        df = pd.DataFrame(self.summary)
        order = ["ok", "no_stations", "no_cf", "exists", "no_shape"]
        counts = df["status"].value_counts().to_dict()

        # 控制台汇总
        logger.info("=" * 60)
        logger.info("运行汇总（按状态统计）:")
        for st in order:
            if st in counts:
                logger.info(f"  {st:<12} {self.STATUS_DESC[st]:<22} : {counts[st]} 条")
        n_ok_sta = int(df.loc[df["status"] == "ok", "n_stations"].sum())
        logger.info(f"  生成文件 {counts.get('ok', 0)} 个，合计场站 {n_ok_sta} 个")

        # 重点列出"未生成文件"的组合（最常见的是无场站）
        missing = df[df["status"] != "ok"].sort_values(["status", "region", "type"])
        if not missing.empty:
            logger.info("-" * 60)
            logger.info("未生成文件的 区域×类型（原因）:")
            for _, r in missing.iterrows():
                logger.info(
                    f"  {r['region']:<22} {r['type']:<6} "
                    f"{self.STATUS_DESC[r['status']]}"
                )

        # 写 CSV 报告
        tag = f"{self.source}_{df['model'].iloc[0]}_{self.scenario}"
        if self.years:
            tag += "_" + "-".join(str(y) for y in self.years)
        report_path = os.path.join(self.output_dir, f"run_summary_{tag}.csv")
        os.makedirs(self.output_dir, exist_ok=True)
        df_out = df[["region", "type", "scenario", "model", "status", "n_stations"]].copy()
        df_out["status_desc"] = df_out["status"].map(self.STATUS_DESC)
        df_out.to_csv(report_path, index=False)
        logger.info(f"汇总报告已写出: {report_path}")
        logger.info("=" * 60)

    # ------------------------------------------------------------------
    # BCSD
    # ------------------------------------------------------------------

    def _run_bcsd(self):
        model, scenario, cfs_dir = self.model, self.scenario, self.cfs_dir
        solar_base = os.path.join(cfs_dir, CF_SUBDIR["bcsd"]["solar"], model)
        if not os.path.isdir(solar_base):
            logger.error(f"BCSD solar 目录不存在: {solar_base}")
            return

        regions = sorted(os.listdir(solar_base))
        if self.region:
            if self.region in regions:
                regions = [self.region]
                logger.info(f"BCSD 仅处理指定区域: {self.region}")
            else:
                logger.error(f"指定区域 '{self.region}' 不存在，可用: {regions}")
                return
        else:
            logger.info(f"BCSD 共 {len(regions)} 个区域: {regions}")

        for region in regions:
            logger.info(f"===== 处理 BCSD 区域: {region} =====")
            self._process_bcsd_region(region, model, scenario, cfs_dir)

    def _process_bcsd_region(self, region, model, scenario, cfs_dir):
        ne_name = bcsd_region_to_ne_name(region)
        if ne_name not in self.country_shapes:
            logger.warning(f"  区域 '{region}' 在 shapefile 中无匹配，跳过")
            self._record(region, "solar", "no_shape")
            self._record(region, "wind", "no_shape")
            return
        country_geom = self.country_shapes[ne_name]

        for stype in ["solar", "wind"]:
            logger.info(f"  --- {stype} ---")
            cf_path = find_cf_file(cfs_dir, "bcsd", stype, model, region, scenario)
            if cf_path is None:
                logger.warning(f"  未找到 CF 文件: {stype}/{model}/{region}/{scenario}")
                self._record(region, stype, "no_cf")
                continue

            out_path = self._get_output_path(stype, region, model, scenario, "bcsd")
            if self._should_skip(out_path):
                self._record(region, stype, "exists")
                continue

            logger.info(f"  CF 文件: {cf_path}")
            stations = self._filter_stations_for_country(ne_name, country_geom, stype)
            if stations.empty:
                logger.info(f"  该国家无 {stype} 场站")
                self._record(region, stype, "no_stations")
                continue
            logger.info(f"  共 {len(stations)} 个 {stype} 场站")

            self._compute_and_save(cf_path, stations, stype, region, model,
                                   scenario, source="bcsd")
            self._record(region, stype, "ok", len(stations))

    # ------------------------------------------------------------------
    # China
    # ------------------------------------------------------------------

    def _run_china(self):
        model, scenario, cfs_dir = self.model, self.scenario, self.cfs_dir
        ne_name = "China"
        if ne_name not in self.country_shapes:
            logger.error("shapefile 中未找到 China")
            return
        country_geom = self.country_shapes[ne_name]

        for stype in ["solar", "wind"]:
            logger.info(f"===== China {stype} =====")
            out_path = self._get_output_path(stype, "china", model, scenario, "china")
            if self._should_skip(out_path):
                self._record("china", stype, "exists")
                continue
            cf_path = find_cf_file(cfs_dir, "china", stype, model, "china", scenario)
            if cf_path is None:
                logger.warning(f"  未找到 China CF 文件: {stype}/{model}/{scenario}")
                self._record("china", stype, "no_cf")
                continue
            logger.info(f"  CF 文件: {cf_path}")
            stations = self._filter_stations_for_country(ne_name, country_geom, stype)
            if stations.empty:
                logger.info(f"  中国无 {stype} 场站")
                self._record("china", stype, "no_stations")
                continue
            logger.info(f"  共 {len(stations)} 个 {stype} 场站")
            self._compute_and_save(cf_path, stations, stype, "china", model,
                                   scenario, source="china")
            self._record("china", stype, "ok", len(stations))

    # ------------------------------------------------------------------
    # NAM-12
    # ------------------------------------------------------------------

    def _run_nam12(self):
        if not all([self.gcm, self.realization, self.rcm]):
            logger.error("NAM-12 需要指定 --gcm, --realization, --rcm")
            return
        gcm, realization, rcm = self.gcm, self.realization, self.rcm
        scenario, cfs_dir = self.scenario, self.cfs_dir
        target_countries = ["Canada", "United States of America", "Mexico"]

        for stype in ["solar", "wind"]:
            logger.info(f"===== NAM-12 {stype} =====")
            cf_paths = find_cf_files_nam12(
                cfs_dir, stype, gcm, realization, rcm, scenario, self.years)
            if not cf_paths:
                logger.warning(
                    f"  未找到 NAM-12 CF 文件: "
                    f"{stype}/{gcm}/{realization}/{scenario}/yearly (rcm={rcm})"
                )
                for country_name in target_countries:
                    self._record(country_name, stype, "no_cf")
                continue
            logger.info(
                f"  CF 文件: {len(cf_paths)} 个 "
                f"[{os.path.basename(cf_paths[0])} … {os.path.basename(cf_paths[-1])}]"
            )

            for country_name in target_countries:
                if country_name not in self.country_shapes:
                    self._record(country_name, stype, "no_shape")
                    continue
                out_path = self._get_output_path(
                    stype, country_name, None, scenario, "nam12",
                    gcm=gcm, realization=realization, rcm=rcm)
                if self._should_skip(out_path):
                    self._record(country_name, stype, "exists")
                    continue
                country_geom = self.country_shapes[country_name]
                stations = self._filter_stations_for_country(country_name, country_geom, stype)
                if stations.empty:
                    self._record(country_name, stype, "no_stations")
                    continue
                logger.info(f"  {country_name}: {len(stations)} 个 {stype} 场站")
                self._compute_and_save_nam12(cf_paths, stations, stype, country_name,
                                             gcm, realization, rcm, scenario)
                self._record(country_name, stype, "ok", len(stations))

    # ------------------------------------------------------------------
    # 场站筛选（按国家 + 去重 + activation_year）—— 与 1° 版本一致
    # ------------------------------------------------------------------

    def _filter_stations_for_country(self, country_name, country_geom, stype):
        prepared = prep(country_geom)
        df = self.stations_df
        df_typed = df[df["type"] == stype].copy()
        if df_typed.empty:
            return df_typed

        keep = [prepared.contains(Point(row["lon"], row["lat"]))
                for _, row in df_typed.iterrows()]
        result = df_typed[np.array(keep)].copy()
        if result.empty:
            return result

        result = (
            result.sort_values("year")
            .groupby(["lon", "lat"], as_index=False)
            .agg({"year": "min", "type": "first", "capacity_gw": "first"})
        )
        result = result.rename(columns={"year": "activation_year"}).reset_index(drop=True)
        return result

    # ------------------------------------------------------------------
    # 出力计算（BCSD / China，规则网，最近邻取点）
    # ------------------------------------------------------------------

    def _compute_and_save(self, cf_path, stations, stype, region, model,
                          scenario, source):
        ds = nc.Dataset(cf_path, "r")
        cf_varname = CF_VARNAME[stype]

        cf_lons = ds.variables["lon"][:].astype(np.float64)   # 0–360
        cf_lats = ds.variables["lat"][:].astype(np.float64)

        times_raw = ds.variables["time"][:]
        time_units = ds.variables["time"].units
        times_dt = nc.num2date(times_raw, time_units)
        cf_years = np.array([t.year for t in times_dt], dtype=np.int32)

        station_lons_360 = lon_to_360(stations["lon"].values.astype(np.float64))
        station_lats = stations["lat"].values.astype(np.float64)
        capacities = stations["capacity_gw"].values.astype(np.float32)
        activation_years = stations["activation_year"].values.astype(np.int32)

        n_time = ds.dimensions["time"].size
        n_stations = len(stations)

        # 最近邻索引 + 距离
        lat_idx, lon_idx, dist = nearest_index_regular(
            cf_lons, cf_lats, station_lons_360, station_lats)

        n_far = int(np.sum(dist > self.max_dist))
        logger.info(
            f"  时间步={n_time}, 场站={n_stations}; "
            f"最近邻距离 median={np.median(dist):.4f}° max={dist.max():.4f}°; "
            f"超容差({self.max_dist}°)场站={n_far}"
        )
        valid_station = dist <= self.max_dist

        # 仅计算指定年份：构建输出时间步映射
        sel = self._select_time_indices(cf_years)
        if sel is not None:
            logger.info(f"  仅计算年份 {self.years}: 选中 {len(sel)}/{n_time} 个时间步")
            n_out = len(sel)
            out_pos = np.full(n_time, -1, dtype=np.int64)
            out_pos[sel] = np.arange(n_out)
        else:
            n_out = n_time

        power = np.full((n_out, n_stations), np.nan, dtype=np.float32)

        # 仅在最近格点周围的空间子区间内读取（减小 IO）
        lat_lo, lat_hi = int(lat_idx.min()), int(lat_idx.max()) + 1
        lon_lo, lon_hi = int(lon_idx.min()), int(lon_idx.max()) + 1
        lat_rel = lat_idx - lat_lo
        lon_rel = lon_idx - lon_lo

        chunk_size = 1000
        for t_start in tqdm(range(0, n_time, chunk_size),
                            desc="  计算出力", unit="chunk"):
            t_end = min(t_start + chunk_size, n_time)

            # 指定年份时，跳过完全不含目标年份的时间块（省 IO）
            if sel is not None:
                chunk_pos = out_pos[t_start:t_end]
                keep_local = chunk_pos >= 0
                if not keep_local.any():
                    continue

            chunk_years = cf_years[t_start:t_end]

            cf_slab = ds.variables[cf_varname][t_start:t_end, lat_lo:lat_hi, lon_lo:lon_hi]
            cf_slab = np.ma.filled(cf_slab.astype(np.float32), np.nan)

            # 向量化取各场站最近格点: (chunk, n_stations)
            cf_pts = cf_slab[:, lat_rel, lon_rel]

            # 年份掩码: CF年份 >= activation_year
            year_ok = chunk_years[:, None] >= activation_years[None, :]
            cf_pts = np.where(year_ok, cf_pts, np.nan)

            # 超容差场站整列置 NaN
            cf_pts[:, ~valid_station] = np.nan

            block = np.where(
                np.isnan(cf_pts), np.nan, cf_pts * capacities[None, :]
            ).astype(np.float32)

            if sel is None:
                power[t_start:t_end, :] = block
            else:
                power[chunk_pos[keep_local], :] = block[keep_local, :]

        ds.close()
        self._save_output(power, stations, stype, region, model, scenario,
                          source, cf_path, dist, time_indices=sel)

    # ------------------------------------------------------------------
    # 出力计算（NAM-12，旋转极网，最近邻取点）
    # ------------------------------------------------------------------

    def _compute_and_save_nam12(self, cf_paths, stations, stype, country_name,
                                gcm, realization, rcm, scenario):
        cf_varname = CF_VARNAME[stype]

        # 网格与最近邻索引：各年份文件网格一致，用第一个文件计算一次即可
        ds0 = nc.Dataset(cf_paths[0], "r")
        lat2d = ds0.variables["lat"][:].astype(np.float64)
        lon2d = ds0.variables["lon"][:].astype(np.float64)
        ds0.close()
        lon2d_180 = lon_to_180(lon2d)

        station_lons_180 = lon_to_180(lon_to_360(stations["lon"].values.astype(np.float64)))
        station_lats = stations["lat"].values.astype(np.float64)
        capacities = stations["capacity_gw"].values.astype(np.float32)
        activation_years = stations["activation_year"].values.astype(np.int32)
        n_stations = len(stations)

        rlat_idx, rlon_idx, dist = nearest_index_2d(
            lat2d, lon2d_180, station_lons_180, station_lats)
        valid_station = dist <= self.max_dist

        # 跨文件汇总时间轴（按年份分文件，逐小时）
        per_file = []           # (path, n_time, years_array)
        time_units = None
        for path in cf_paths:
            ds = nc.Dataset(path, "r")
            times_raw = ds.variables["time"][:]
            tunits = ds.variables["time"].units
            ds.close()
            if time_units is None:
                time_units = tunits
            times_dt = nc.num2date(times_raw, tunits)
            yrs = np.array([t.year for t in times_dt], dtype=np.int32)
            per_file.append((path, len(times_raw), yrs, np.asarray(times_raw)))

        cf_years = np.concatenate([p[2] for p in per_file])
        all_times = np.concatenate([p[3] for p in per_file])
        n_time_total = len(cf_years)

        n_far = int(np.sum(dist > self.max_dist))
        logger.info(
            f"  NAM-12: 文件={len(cf_paths)}, 总时间步={n_time_total}, 场站={n_stations}; "
            f"最近邻距离 median={np.median(dist):.4f}° max={dist.max():.4f}°; "
            f"超容差({self.max_dist}°)场站={n_far}"
        )

        # 仅计算指定年份：在拼接后的全局时间轴上构建输出时间步映射
        sel = self._select_time_indices(cf_years)
        if sel is not None:
            logger.info(
                f"  仅计算年份 {self.years}: 选中 {len(sel)}/{n_time_total} 个时间步")
            n_out = len(sel)
            out_pos = np.full(n_time_total, -1, dtype=np.int64)
            out_pos[sel] = np.arange(n_out)
        else:
            n_out = n_time_total

        power = np.full((n_out, n_stations), np.nan, dtype=np.float32)
        chunk_size = 500
        global_off = 0
        for path, n_time, yrs, _ in per_file:
            ds = nc.Dataset(path, "r")
            for t_start in tqdm(range(0, n_time, chunk_size),
                                desc=f"  NAM-12 {os.path.basename(path)}",
                                unit="chunk"):
                t_end = min(t_start + chunk_size, n_time)
                g_start, g_end = global_off + t_start, global_off + t_end

                if sel is not None:
                    chunk_pos = out_pos[g_start:g_end]
                    keep_local = chunk_pos >= 0
                    if not keep_local.any():
                        continue

                chunk_years = yrs[t_start:t_end]

                cf_chunk = ds.variables[cf_varname][t_start:t_end, :, :]
                cf_chunk = np.ma.filled(cf_chunk.astype(np.float32), np.nan)

                cf_pts = cf_chunk[:, rlat_idx, rlon_idx]   # (chunk, n_stations)

                year_ok = chunk_years[:, None] >= activation_years[None, :]
                cf_pts = np.where(year_ok, cf_pts, np.nan)
                cf_pts[:, ~valid_station] = np.nan

                block = np.where(
                    np.isnan(cf_pts), np.nan, cf_pts * capacities[None, :]
                ).astype(np.float32)

                if sel is None:
                    power[g_start:g_end, :] = block
                else:
                    power[chunk_pos[keep_local], :] = block[keep_local, :]
            ds.close()
            global_off += n_time

        self._save_output_nam12(power, stations, stype, country_name,
                                gcm, realization, rcm, scenario, dist,
                                all_times, time_units, time_indices=sel)

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------

    def _get_output_path(self, stype, region, model, scenario, source,
                         gcm=None, realization=None, rcm=None):
        prefix = "pv" if stype == "solar" else "wind"
        if source == "nam12":
            dirname = os.path.join(self.output_dir, f"{prefix}_out_NAM-12", gcm, realization)
            fname = (f"{prefix}_stations_out_NAM-12"
                     f"_{gcm}_{realization}_{rcm}_{scenario}_allmonths.nc")
        else:
            dirname = os.path.join(self.output_dir, f"{prefix}_out", model, region)
            fname = f"{prefix}_stations_out_{region}_{model}_{scenario}_allmonths.nc"
        os.makedirs(dirname, exist_ok=True)
        return os.path.join(dirname, fname)

    def _should_skip(self, out_path):
        if self.overwrite:
            return False
        if os.path.isfile(out_path):
            logger.info(f"  跳过（文件已存在）: {out_path}")
            return True
        return False

    def _write_common_vars(self, ds_out, power, stations, cf_path, dist,
                           time_indices=None, times=None, time_units=None):
        """写入维度、坐标、出力等公共变量。

        time_indices 非 None 时，仅写入这些时间步（与 power 的时间维一致）。
        times/time_units 已给定时直接使用（NAM-12 跨多个年份文件拼接），
        否则从单个 cf_path 读取（BCSD / China）。
        """
        if times is None:
            ds_cf = nc.Dataset(cf_path, "r")
            times = ds_cf.variables["time"][:]
            time_units = ds_cf.variables["time"].units
            ds_cf.close()

        if time_indices is not None:
            times = times[time_indices]

        n_time, n_stations = power.shape
        ds_out.createDimension("time", n_time)
        ds_out.createDimension("id", n_stations)

        time_var = ds_out.createVariable("time", "f8", ("time",))
        time_var.units = time_units
        time_var[:] = times

        lon_var = ds_out.createVariable("station_lon", "f4", ("id",))
        lon_var.units = "degrees_east"
        lon_var[:] = stations["lon"].values.astype(np.float32)

        lat_var = ds_out.createVariable("station_lat", "f4", ("id",))
        lat_var.units = "degrees_north"
        lat_var[:] = stations["lat"].values.astype(np.float32)

        type_var = ds_out.createVariable("station_type", "i1", ("id",))
        type_var[:] = np.where(stations["type"] == "solar", 0, 1).astype(np.int8)

        cap_var = ds_out.createVariable("capacity_gw", "f4", ("id",))
        cap_var.units = "GW"
        cap_var[:] = stations["capacity_gw"].values.astype(np.float32)

        act_var = ds_out.createVariable("activation_year", "i4", ("id",))
        act_var.units = "year"
        act_var.long_name = "场站激活年份（出力从此年开始有效）"
        act_var[:] = stations["activation_year"].values.astype(np.int32)

        # 最近邻匹配距离（便于诊断网格一致性）
        md_var = ds_out.createVariable("match_dist_deg", "f4", ("id",))
        md_var.units = "degrees"
        md_var.long_name = "场站中心到最近 CF 格点的距离"
        md_var[:] = dist.astype(np.float32)

        power_var = ds_out.createVariable(
            "power", "f4", ("time", "id"),
            zlib=True, complevel=4, fill_value=np.nan)
        power_var.units = "GW"
        power_var[:] = power

    def _save_output(self, power, stations, stype, region, model,
                     scenario, source, cf_path, dist, time_indices=None):
        out_path = self._get_output_path(stype, region, model, scenario, source)
        logger.info(f"  保存: {out_path}")
        try:
            ds_out = nc.Dataset(out_path, "w", format="NETCDF4")
            self._write_common_vars(ds_out, power, stations, cf_path, dist,
                                    time_indices=time_indices)
            ds_out.region = region
            ds_out.scenario = scenario
            ds_out.source_csv = os.path.basename(self.csv_path)
            ds_out.grid_resolution = "0.1deg"
            ds_out.match_method = "nearest"
            ds_out.max_match_dist_deg = float(self.max_dist)
            if self.years:
                ds_out.computed_years = ",".join(str(y) for y in self.years)
            ds_out.close()
            logger.info(f"  完成: {out_path}")
        except Exception as e:
            logger.error(f"  保存失败，清理损坏文件: {out_path}\n  错误: {e}")
            if os.path.isfile(out_path):
                os.remove(out_path)
            raise

    def _save_output_nam12(self, power, stations, stype, country_name,
                           gcm, realization, rcm, scenario, dist,
                           times, time_units, time_indices=None):
        out_path = self._get_output_path(
            stype, country_name, None, scenario, "nam12",
            gcm=gcm, realization=realization, rcm=rcm)
        logger.info(f"  保存: {out_path}")
        try:
            ds_out = nc.Dataset(out_path, "w", format="NETCDF4")
            self._write_common_vars(ds_out, power, stations, None, dist,
                                    time_indices=time_indices,
                                    times=times, time_units=time_units)
            ds_out.region = country_name
            ds_out.scenario = scenario
            ds_out.source_csv = os.path.basename(self.csv_path)
            ds_out.gcm = gcm
            ds_out.realization = realization
            ds_out.rcm = rcm
            ds_out.grid_resolution = "0.1deg"
            ds_out.match_method = "nearest"
            ds_out.max_match_dist_deg = float(self.max_dist)
            if self.years:
                ds_out.computed_years = ",".join(str(y) for y in self.years)
            ds_out.close()
            logger.info(f"  完成: {out_path}")
        except Exception as e:
            logger.error(f"  保存失败，清理损坏文件: {out_path}\n  错误: {e}")
            if os.path.isfile(out_path):
                os.remove(out_path)
            raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="0.1° 场站出力计算（最近邻取点）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--csv", required=True,
                        help="场站选址 CSV 文件路径 (stations_SSP*.csv)")
    parser.add_argument("--source", required=True, choices=["bcsd", "china", "nam12"],
                        help="CF 数据源类型: bcsd / china / nam12")
    parser.add_argument("--model", help="气候模型名")
    parser.add_argument("--scenario",
                        help="排放情景代码；不指定则从 CSV 文件名推断")
    parser.add_argument("--shp",
                        default="data/maps/natural_earth/ne_110m_admin_0_countries.shp",
                        help="国家边界矢量文件路径")
    parser.add_argument("--cfs-dir", default="data/cfs", help="CF 数据根目录")
    parser.add_argument("--output-dir", default="outputs/outputs_0p1deg",
                        help="输出根目录 (默认: outputs/outputs_0p1deg)")
    parser.add_argument("--gcm", help="NAM-12 GCM 名 (仅 nam12)")
    parser.add_argument("--realization", help="NAM-12 realization (仅 nam12)")
    parser.add_argument("--rcm", help="NAM-12 RCM 名 (仅 nam12)")
    parser.add_argument("--region", help="BCSD 区域名，仅处理指定国家/区域")
    parser.add_argument("--overwrite", action="store_true", default=False,
                        help="覆盖已有输出文件（默认跳过，支持断点续跑）")
    parser.add_argument("--max-dist", type=float, default=DEFAULT_MAX_DIST,
                        help=f"最近邻匹配距离容差（度，默认 {DEFAULT_MAX_DIST}）")
    parser.add_argument("--years", type=int, nargs="+", default=None,
                        metavar="YEAR",
                        help="仅计算指定日历年份的发电量，如 --years 2030 2040 2050；"
                             "不指定则计算 CF 文件全部年份")

    args = parser.parse_args()
    if args.scenario is None:
        args.scenario = infer_scenario_from_csv(args.csv)
        logger.info(f"自动推断情景: {args.scenario}")
    if args.source != "nam12" and args.model is None:
        parser.error("非 nam12 数据源需要指定 --model")
    return args


def main():
    args = parse_args()
    calc = StationOutputCalculator0p1(
        csv_path=args.csv, source=args.source, model=args.model,
        scenario=args.scenario, shp_path=args.shp, cfs_dir=args.cfs_dir,
        output_dir=args.output_dir, gcm=args.gcm, realization=args.realization,
        rcm=args.rcm, region=args.region, overwrite=args.overwrite,
        max_dist=args.max_dist, years=args.years)
    calc.run()
    logger.info("全部完成。")


if __name__ == "__main__":
    main()
