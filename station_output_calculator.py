#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
场站出力计算模块

基于场站选址结果（stations_SSP*.csv）、风光容量因子数据，
计算各场站的逐时出力，并输出为 NetCDF 文件。

用法示例:
# BCSD - 全部国家
python station_output_calculator.py \
    --csv data/stations/stations_SSP1-2.6.csv \
    --source bcsd --model MIROC-ES2H --scenario ssp126

# BCSD - 仅 Germany
python station_output_calculator.py \
    --csv data/stations/stations_SSP1-2.6.csv \
    --source bcsd --model MIROC-ES2H --scenario ssp126 \
    --region Germany

# China
python station_output_calculator.py \
    --csv data/stations/stations_SSP2-4.5.csv \
    --source china --model MIROC-ES2H --scenario ssp245

# NAM-12
python station_output_calculator.py \
    --csv data/stations/stations_SSP5-6.0.csv \
    --source nam12 --gcm MPI-ESM1-2-LR --realization r1i1p1f1 --rcm CRCM5
"""

import argparse
import os
import re
import sys
import logging

import numpy as np
import pandas as pd
import netCDF4 as nc
from shapely.geometry import Point, shape
from shapely.prepared import prep
import shapefile
from tqdm import tqdm

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# SSP 场景映射：CSV 文件名中的名称 → CF 文件中的代码
SSP_MAP = {
    "SSP1-2.6": "ssp126",
    "SSP2-4.5": "ssp245",
    "SSP5-6.0": "ssp585",   # CSV 文件命名为 SSP5-6.0，对应 CF 中的 ssp585
    "SSP5-8.5": "ssp585",
}

# CF 数据源目录后缀及变量名
CF_SUBDIR = {
    "bcsd": {"solar": "CFs_of_solar", "wind": "CFs_of_wind"},
    "china": {"solar": "CFs_of_solar_china", "wind": "CFs_of_wind_china"},
    "nam12": {"solar": "CFs_of_solar_NAM-12", "wind": "CFs_of_wind_NAM-12"},
}

CF_VARNAME = {
    "solar": "solar_cf",
    "wind": "wind_cf",
}

# BCSD 国家目录名 → Natural Earth shapefile NAME 字段映射
BCSD_REGION_TO_NAME = {
    "South-Africa": "South Africa",
    "South-Korea": "South Korea",
    "United-Kingdom": "United Kingdom",
    "México": "Mexico",
}

# 年份区间 → 场站年份的映射（用于 reference）
# 实际逻辑通过 activation_year 实现：场站出力仅在 cf_year >= activation_year 时有效
# YEAR_TO_STATION_YEAR = {2030..2039: 2030, 2040..2049: 2040, 2050..2060: 2050}

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
    """将经度从 [-180, 180] 转换为 [0, 360]。"""
    return lon % 360


def lon_to_180(lon):
    """将经度从 [0, 360] 转换为 [-180, 180]。"""
    return ((lon + 180) % 360) - 180


def infer_scenario_from_csv(csv_path):
    """从 CSV 文件名推断 SSP 情景代码。

    例如 stations_SSP1-2.6.csv → ssp126
    """
    basename = os.path.basename(csv_path)
    for ssp_name, ssp_code in SSP_MAP.items():
        if ssp_name in basename:
            return ssp_code
    raise ValueError(
        f"无法从文件名 '{basename}' 推断 SSP 情景，"
        f"支持的名称: {list(SSP_MAP.keys())}"
    )


def load_country_shapes(shp_path):
    """读取 Natural Earth 国家边界 shapefile，返回 {country_name: shapely geometry} 字典。"""
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
    """将 BCSD 目录中的区域名转为 Natural Earth 的 NAME 字段。"""
    if region_dir in BCSD_REGION_TO_NAME:
        return BCSD_REGION_TO_NAME[region_dir]
    return region_dir


def find_cf_file(cfs_dir, source, stype, model, region, scenario):
    """查找 CF 文件路径。

    Parameters
    ----------
    cfs_dir : str  - CF 数据根目录
    source : str   - "bcsd" / "china" / "nam12"
    stype : str    - "solar" / "wind"
    model : str    - 模型名
    region : str   - 区域名
    scenario : str - ssp126 / ssp245 / ssp585

    Returns
    -------
    str or None : 找到的文件路径，未找到返回 None
    """
    subdir = CF_SUBDIR[source][stype]

    # China 数据源: 文件直接在 model 目录下，无 region 子目录
    # 例如: data/cfs/CFs_of_solar_china/MIROC-ES2H/solar_CF_china_MIROC-ES2H_ssp126_2015-2060_allmonths.nc
    if source == "china":
        base = os.path.join(cfs_dir, subdir, model)
    else:
        base = os.path.join(cfs_dir, subdir, model, region)

    if not os.path.isdir(base):
        return None

    # 查找匹配的 .nc 文件
    candidates = []
    for f in os.listdir(base):
        if not f.endswith(".nc"):
            continue
        if scenario in f and "allmonths" in f:
            # 优先选择 years 范围最大的文件
            candidates.append(f)

    if not candidates:
        # 退回：任何包含 scenario 的文件
        for f in os.listdir(base):
            if not f.endswith(".nc"):
                continue
            if scenario in f:
                candidates.append(f)

    if not candidates:
        return None

    # 选择覆盖年份范围最广的文件
    def year_span(fname):
        m = re.search(r'_(\d{4})-(\d{4})_', fname)
        if m:
            return int(m.group(2)) - int(m.group(1))
        return 0
    candidates.sort(key=year_span, reverse=True)
    return os.path.join(base, candidates[0])


def find_cf_file_nam12(cfs_dir, stype, gcm, realization, rcm, scenario):
    """查找 NAM-12 CF 文件路径。"""
    subdir = CF_SUBDIR["nam12"][stype]
    base = os.path.join(cfs_dir, subdir, gcm, realization)

    if not os.path.isdir(base):
        return None

    for f in os.listdir(base):
        if not f.endswith(".nc"):
            continue
        if scenario in f and "allmonths" in f and rcm in f:
            return os.path.join(base, f)

    # 退回
    for f in os.listdir(base):
        if not f.endswith(".nc"):
            continue
        if scenario in f and rcm in f:
            return os.path.join(base, f)

    return None


def get_cf_grid_points_in_station_bbox(nc_path, var_name, station_lon_360,
                                        station_lat, cf_lon_1d, cf_lat_1d):
    """获取场站 bbox 内的 CF 格点索引。

    Parameters
    ----------
    nc_path : str  - CF NetCDF 文件路径（仅用于判断是否需要打开文件）
    var_name : str - CF 变量名
    station_lon_360 : float - 场站经度（0-360）
    station_lat : float - 场站纬度
    cf_lon_1d : np.ndarray - CF 经度数组（0-360）
    cf_lat_1d : np.ndarray - CF 纬度数组

    Returns
    -------
    lat_idx, lon_idx : np.ndarray - bbox 内格点的索引数组
    """
    # bbox: [lon-0.5, lon+0.5) × [lat-0.5, lat+0.5)
    lon_min = station_lon_360 - 0.5
    lon_max = station_lon_360 + 0.5
    lat_min = station_lat - 0.5
    lat_max = station_lat + 0.5

    # 处理经度跨越 0°/360° 的情况（如 station_lon_360 ≈ 0 或 360）
    if lon_min < 0:
        # 左闭右开 [lon_min+360, 360) ∪ [0, lon_max)
        mask_lon = (cf_lon_1d >= (lon_min + 360)) | (cf_lon_1d < lon_max)
    elif lon_max > 360:
        # [lon_min, 360) ∪ [0, lon_max-360)
        mask_lon = (cf_lon_1d >= lon_min) | (cf_lon_1d < (lon_max - 360))
    else:
        mask_lon = (cf_lon_1d >= lon_min) & (cf_lon_1d < lon_max)

    mask_lat = (cf_lat_1d >= lat_min) & (cf_lat_1d < lat_max)

    lat_idx = np.where(mask_lat)[0]
    lon_idx = np.where(mask_lon)[0]

    return lat_idx, lon_idx


def filter_points_by_country(lat_idx, lon_idx, cf_lats, cf_lons,
                             country_geom, prepared_geom):
    """从 bbox 内的 CF 格点中筛选在国家边界内的点。

    Parameters
    ----------
    lat_idx, lon_idx : 索引数组
    cf_lats, cf_lons : CF 的 lat/lon 一维数组
    country_geom : shapely geometry
    prepared_geom : prepared shapely geometry

    Returns
    -------
    filtered_lat_idx, filtered_lon_idx : 筛选后的索引
    """
    if len(lat_idx) == 0 or len(lon_idx) == 0:
        return np.array([], dtype=int), np.array([], dtype=int)

    # 生成网格索引
    lat_grid, lon_grid = np.meshgrid(lat_idx, lon_idx, indexing="ij")
    lat_flat = lat_grid.ravel()
    lon_flat = lon_grid.ravel()

    # 获取实际坐标
    points_lat = cf_lats[lat_flat]
    points_lon = cf_lons[lon_flat]

    # 转为 -180~180 用于 contains 检测
    points_lon_180 = lon_to_180(points_lon)

    # 逐点检测（向量化优化：先用 bbox 粗筛）
    keep = np.zeros(len(lat_flat), dtype=bool)
    for i in range(len(lat_flat)):
        pt = Point(points_lon_180[i], points_lat[i])
        if prepared_geom.contains(pt):
            keep[i] = True

    return lat_flat[keep], lon_flat[keep]


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class StationOutputCalculator:
    """场站出力计算器。

    读取场站选址 CSV、CF 容量因子 NetCDF 和国家边界矢量，
    计算各场站的逐时出力并保存为 NetCDF。

    Parameters
    ----------
    csv_path : str       - 场站选址 CSV 文件路径
    source : str         - 数据源: "bcsd" / "china" / "nam12"
    model : str          - 气候模型名
    scenario : str       - 排放情景代码 (ssp126 / ssp245 / ssp585)
    shp_path : str       - 国家边界矢量文件路径
    cfs_dir : str        - CF 数据根目录
    output_dir : str     - 输出根目录
    gcm : str, optional  - NAM-12 GCM 名
    realization : str, optional - NAM-12 realization
    rcm : str, optional  - NAM-12 RCM 名
    """

    def __init__(self, csv_path, source, model, scenario, shp_path,
                 cfs_dir, output_dir, gcm=None, realization=None, rcm=None,
                 region=None, overwrite=False):
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
        self.region = region  # BCSD 区域过滤（目录名，如 Germany）
        self.overwrite = overwrite  # 是否覆盖已有输出文件

        # 加载场站数据
        self.stations_df = pd.read_csv(csv_path)
        logger.info(
            f"加载场站数据: {len(self.stations_df)} 条记录, "
            f"情景={scenario}"
        )

        # 加载国家边界
        self.country_shapes = load_country_shapes(shp_path)
        logger.info(f"加载国家边界: {len(self.country_shapes)} 个国家/地区")

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def run(self):
        """执行所有计算并输出结果。"""
        if self.source == "china":
            self._run_china()
        elif self.source == "nam12":
            self._run_nam12()
        else:
            self._run_bcsd()

    # ------------------------------------------------------------------
    # BCSD 处理
    # ------------------------------------------------------------------

    def _run_bcsd(self):
        """处理 BCSD 数据源：按国家分组处理。"""
        model = self.model
        scenario = self.scenario
        cfs_dir = self.cfs_dir

        # 获取 BCSD 中可用的国家/区域目录
        solar_base = os.path.join(cfs_dir, CF_SUBDIR["bcsd"]["solar"], model)
        if not os.path.isdir(solar_base):
            logger.error(f"BCSD solar 目录不存在: {solar_base}")
            return

        regions = sorted(os.listdir(solar_base))

        # 如果指定了 region，只处理该区域
        if self.region:
            if self.region in regions:
                regions = [self.region]
                logger.info(f"BCSD 仅处理指定区域: {self.region}")
            else:
                logger.error(
                    f"指定区域 '{self.region}' 不存在，"
                    f"可用区域: {regions}"
                )
                return
        else:
            logger.info(f"BCSD 共 {len(regions)} 个区域: {regions}")

        for region in regions:
            logger.info(f"===== 处理 BCSD 区域: {region} =====")
            self._process_bcsd_region(region, model, scenario, cfs_dir)

    def _process_bcsd_region(self, region, model, scenario, cfs_dir):
        """处理单个 BCSD 区域。"""
        # 确定国家名（用于边界筛选）
        ne_name = bcsd_region_to_ne_name(region)
        if ne_name not in self.country_shapes:
            logger.warning(f"  区域 '{region}' 在 shapefile 中无匹配，跳过")
            return
        country_geom = self.country_shapes[ne_name]

        # 分别处理 solar 和 wind
        for stype in ["solar", "wind"]:
            logger.info(f"  --- {stype} ---")

            # 查找 CF 文件
            cf_path = find_cf_file(cfs_dir, "bcsd", stype, model, region, scenario)
            if cf_path is None:
                logger.warning(f"  未找到 CF 文件: {stype}/{model}/{region}/{scenario}")
                continue

            # 检查输出文件是否已存在
            out_path = self._get_output_path(stype, region, model, scenario, "bcsd")
            if self._should_skip(out_path):
                continue

            logger.info(f"  CF 文件: {cf_path}")

            # 筛选该国家的场站（根据场站类型）
            stations = self._filter_stations_for_country(ne_name, country_geom, stype)
            if stations.empty:
                logger.info(f"  该国家无 {stype} 场站")
                continue
            logger.info(f"  共 {len(stations)} 个 {stype} 场站")

            # 计算出力
            self._compute_and_save(
                cf_path, stations, stype, region, model, scenario,
                source="bcsd",
            )

    # ------------------------------------------------------------------
    # China 处理
    # ------------------------------------------------------------------

    def _run_china(self):
        """处理 China 数据源。"""
        model = self.model
        scenario = self.scenario
        cfs_dir = self.cfs_dir

        ne_name = "China"
        if ne_name not in self.country_shapes:
            logger.error("shapefile 中未找到 China")
            return
        country_geom = self.country_shapes[ne_name]

        for stype in ["solar", "wind"]:
            logger.info(f"===== China {stype} =====")

            # 检查输出文件是否已存在
            out_path = self._get_output_path(
                stype, "china", model, scenario, "china"
            )
            if self._should_skip(out_path):
                continue

            cf_path = find_cf_file(cfs_dir, "china", stype, model, "china", scenario)
            if cf_path is None:
                logger.warning(f"  未找到 China CF 文件: {stype}/{model}/{scenario}")
                continue
            logger.info(f"  CF 文件: {cf_path}")

            stations = self._filter_stations_for_country(ne_name, country_geom, stype)
            if stations.empty:
                logger.info(f"  中国无 {stype} 场站")
                continue
            logger.info(f"  共 {len(stations)} 个 {stype} 场站")

            self._compute_and_save(
                cf_path, stations, stype, "china", model, scenario,
                source="china",
            )

    # ------------------------------------------------------------------
    # NAM-12 处理
    # ------------------------------------------------------------------

    def _run_nam12(self):
        """处理 NAM-12 数据源。"""
        if not all([self.gcm, self.realization, self.rcm]):
            logger.error("NAM-12 需要指定 --gcm, --realization, --rcm")
            return

        gcm = self.gcm
        realization = self.realization
        rcm = self.rcm
        scenario = self.scenario
        cfs_dir = self.cfs_dir

        # NAM-12 覆盖北美（加拿大、美国、墨西哥）
        target_countries = ["Canada", "United States of America", "Mexico"]

        for stype in ["solar", "wind"]:
            logger.info(f"===== NAM-12 {stype} =====")
            cf_path = find_cf_file_nam12(
                cfs_dir, stype, gcm, realization, rcm, scenario
            )
            if cf_path is None:
                logger.warning(
                    f"  未找到 NAM-12 CF 文件: {stype}/{gcm}/{realization}/{rcm}/{scenario}"
                )
                continue
            logger.info(f"  CF 文件: {cf_path}")

            for country_name in target_countries:
                if country_name not in self.country_shapes:
                    continue

                # 检查输出文件是否已存在
                out_path = self._get_output_path(
                    stype, country_name, None, scenario, "nam12",
                    gcm=gcm, realization=realization, rcm=rcm,
                )
                if self._should_skip(out_path):
                    continue

                country_geom = self.country_shapes[country_name]

                stations = self._filter_stations_for_country(
                    country_name, country_geom, stype
                )
                if stations.empty:
                    continue
                logger.info(
                    f"  {country_name}: {len(stations)} 个 {stype} 场站"
                )

                self._compute_and_save_nam12(
                    cf_path, stations, stype, country_name,
                    gcm, realization, rcm, scenario,
                )

    # ------------------------------------------------------------------
    # 场站筛选
    # ------------------------------------------------------------------

    def _filter_stations_for_country(self, country_name, country_geom, stype):
        """筛选属于指定国家的、指定类型的场站。

        判断标准：场站格元中心点 (lon, lat) 落在国家边界多边形内。

        去重：同一 (lon, lat, type) 的场站可能出现在多个年份（2030/2040/2050），
        去重后保留最早年份作为激活年份（activation_year）。
        """
        prepared = prep(country_geom)
        df = self.stations_df

        # 类型筛选
        mask_type = df["type"] == stype
        df_typed = df[mask_type].copy()

        if df_typed.empty:
            return df_typed

        # 国家筛选：检查每个场站中心点是否在国家边界内
        keep = []
        for _, row in df_typed.iterrows():
            pt = Point(row["lon"], row["lat"])
            if prepared.contains(pt):
                keep.append(True)
            else:
                keep.append(False)

        result = df_typed[np.array(keep)].copy()

        # 去重：按 (lon, lat) 分组，保留最早年份
        result = (
            result
            .sort_values("year")  # 按年份排序，最早的在前
            .groupby(["lon", "lat"], as_index=False)
            .agg({
                "year": "min",           # 激活年份（最早）
                "type": "first",
                "capacity_gw": "first",  # 容量相同
            })
        )
        result = result.rename(columns={"year": "activation_year"}).reset_index(drop=True)

        return result

    # ------------------------------------------------------------------
    # 出力计算与保存（BCSD / China）
    # ------------------------------------------------------------------

    def _compute_and_save(self, cf_path, stations, stype, region, model,
                          scenario, source):
        """计算场站出力并保存为 NetCDF。

        适用于 BCSD 和 China 数据源（规则经纬度网格）。
        """
        # 打开 CF 文件
        ds = nc.Dataset(cf_path, "r")
        cf_varname = CF_VARNAME[stype]

        # 读取坐标
        cf_lons = ds.variables["lon"][:].astype(np.float64)
        cf_lats = ds.variables["lat"][:].astype(np.float64)

        # 读取时间并转换为日历年
        times_raw = ds.variables["time"][:]
        time_units = ds.variables["time"].units
        times_dt = nc.num2date(times_raw, time_units)
        cf_years = np.array([t.year for t in times_dt], dtype=np.int32)

        # CF 经度是 0-360，场站经度是 -180~180
        # 将场站经度转为 0-360
        station_lons_360 = lon_to_360(stations["lon"].values.astype(np.float64))
        station_lats = stations["lat"].values.astype(np.float64)
        capacities = stations["capacity_gw"].values.astype(np.float32)
        activation_years = stations["activation_year"].values.astype(np.int32)

        # 获取国家边界
        if source == "china":
            ne_name = "China"
        else:
            ne_name = bcsd_region_to_ne_name(region)
        country_geom = self.country_shapes.get(ne_name)
        prepared_geom = prep(country_geom) if country_geom else None

        # 时间维度
        n_time = ds.dimensions["time"].size
        n_stations = len(stations)

        logger.info(f"  时间步数: {n_time}, 场站数: {n_stations}")

        # 预分配输出数组
        power = np.full((n_time, n_stations), np.nan, dtype=np.float32)

        # 为每个场站预处理 CF 索引（bbox + 国界筛选）
        station_cf_masks = []
        for i in range(n_stations):
            slon = station_lons_360[i]
            slat = station_lats[i]

            # bbox 索引
            lat_idx, lon_idx = get_cf_grid_points_in_station_bbox(
                cf_path, cf_varname, slon, slat, cf_lons, cf_lats
            )

            # 国界筛选
            if prepared_geom is not None and len(lat_idx) > 0 and len(lon_idx) > 0:
                lat_idx, lon_idx = filter_points_by_country(
                    lat_idx, lon_idx, cf_lats, cf_lons,
                    country_geom, prepared_geom
                )

            station_cf_masks.append((lat_idx, lon_idx))

            if (i + 1) % 100 == 0:
                logger.info(f"    预处理场站索引: {i + 1}/{n_stations}")

        logger.info("  场站索引预处理完成，开始逐时间步计算...")

        # 分块读取并计算
        chunk_size = 1000  # 每次读取的时间步数
        for t_start in tqdm(range(0, n_time, chunk_size),
                            desc="  计算出力", unit="chunk"):
            t_end = min(t_start + chunk_size, n_time)
            chunk_years = cf_years[t_start:t_end]

            for i in range(n_stations):
                lat_idx, lon_idx = station_cf_masks[i]
                if len(lat_idx) == 0:
                    continue  # 无有效 CF 格点，保持 NaN

                # 年份掩码：只在 CF 年份 >= 激活年份时计算
                act_year = activation_years[i]
                year_mask = chunk_years >= act_year
                if not np.any(year_mask):
                    continue  # 该时间段内场站尚未激活

                # 读取 bbox 内 CF 数据
                cf_data = ds.variables[cf_varname][
                    t_start:t_end,
                    lat_idx.min():lat_idx.max() + 1,
                    lon_idx.min():lon_idx.max() + 1,
                ]

                # 提取对应点：(n_t, n_points)
                lat_rel = lat_idx - lat_idx.min()
                lon_rel = lon_idx - lon_idx.min()
                cf_pts = cf_data[:, lat_rel, lon_rel]  # (chunk_time, n_pts)

                # 沿空间维度取均值（忽略 NaN）
                with np.errstate(all="ignore"):
                    cf_mean = np.nanmean(cf_pts, axis=1).astype(np.float32)

                # 应用年份掩码：未激活的时间步保持 NaN
                cf_mean[~year_mask] = np.nan

                # 所有值都为 NaN 的时间步保持 NaN
                all_nan = np.all(np.isnan(cf_pts), axis=1)
                cf_mean[all_nan] = np.nan

                # 出力 = CF × 容量
                valid_mask = ~np.isnan(cf_mean)
                power[t_start:t_end, i] = np.where(
                    valid_mask, cf_mean * capacities[i], np.nan
                ).astype(np.float32)

        ds.close()

        # 保存
        self._save_output(
            power, stations, stype, region, model, scenario,
            source, cf_path,
        )

    # ------------------------------------------------------------------
    # 出力计算与保存（NAM-12，旋转极网格）
    # ------------------------------------------------------------------

    def _compute_and_save_nam12(self, cf_path, stations, stype, country_name,
                                gcm, realization, rcm, scenario):
        """计算 NAM-12 场站出力并保存。"""
        ds = nc.Dataset(cf_path, "r")
        cf_varname = CF_VARNAME[stype]

        # NAM-12 使用旋转极网格，有 2D lat/lon 辅助坐标
        lat2d = ds.variables["lat"][:].astype(np.float64)    # (rlat, rlon)
        lon2d = ds.variables["lon"][:].astype(np.float64)    # (rlat, rlon)
        n_rlat, n_rlon = lat2d.shape

        # 将 lon2d 转为 0-360（方便统一处理）
        lon2d_180 = lon2d.copy()
        lon2d_360 = lon_to_360(lon2d)

        # 将 2D 坐标展平用于空间索引
        lat_flat = lat2d.ravel()
        lon_flat_360 = lon2d_360.ravel()

        # 读取时间并转换为日历年
        times_raw = ds.variables["time"][:]
        time_units = ds.variables["time"].units
        times_dt = nc.num2date(times_raw, time_units)
        cf_years = np.array([t.year for t in times_dt], dtype=np.int32)

        # 场站坐标
        station_lons_360 = lon_to_360(stations["lon"].values.astype(np.float64))
        station_lats = stations["lat"].values.astype(np.float64)
        capacities = stations["capacity_gw"].values.astype(np.float32)
        activation_years = stations["activation_year"].values.astype(np.int32)

        # 国家边界
        country_geom = self.country_shapes[country_name]
        prepared_geom = prep(country_geom)

        n_time = ds.dimensions["time"].size
        n_stations = len(stations)

        logger.info(f"  NAM-12: 时间步={n_time}, 场站={n_stations}")

        # 预处理每个场站的 CF 格点索引（在旋转网格中的位置）
        station_cf_indices = []  # 每个 station 的 (rlat_idx, rlon_idx) 数组

        for i in range(n_stations):
            slon = station_lons_360[i]
            slat = station_lats[i]

            lon_min = slon - 0.5
            lon_max = slon + 0.5
            lat_min = slat - 0.5
            lat_max = slat + 0.5

            # 处理经度跨越
            if lon_min < 0:
                mask_lon = (lon_flat_360 >= (lon_min + 360)) | (lon_flat_360 < lon_max)
            elif lon_max > 360:
                mask_lon = (lon_flat_360 >= lon_min) | (lon_flat_360 < (lon_max - 360))
            else:
                mask_lon = (lon_flat_360 >= lon_min) & (lon_flat_360 < lon_max)

            mask_lat = (lat_flat >= lat_min) & (lat_flat < lat_max)
            mask = mask_lon & mask_lat

            # 获取在展平数组中的索引
            flat_indices = np.where(mask)[0]
            rlat_idx = flat_indices // n_rlon
            rlon_idx = flat_indices % n_rlon

            # 国界筛选
            if len(rlat_idx) > 0:
                pts_lon_180 = lon2d_180[rlat_idx, rlon_idx]
                pts_lat = lat2d[rlat_idx, rlon_idx]
                keep = np.zeros(len(rlat_idx), dtype=bool)
                for j in range(len(rlat_idx)):
                    if prepared_geom.contains(Point(pts_lon_180[j], pts_lat[j])):
                        keep[j] = True
                rlat_idx = rlat_idx[keep]
                rlon_idx = rlon_idx[keep]

            station_cf_indices.append((rlat_idx, rlon_idx))

        # 分块计算
        power = np.full((n_time, n_stations), np.nan, dtype=np.float32)
        chunk_size = 500

        for t_start in tqdm(range(0, n_time, chunk_size),
                            desc="  NAM-12 计算出力", unit="chunk"):
            t_end = min(t_start + chunk_size, n_time)
            chunk_years = cf_years[t_start:t_end]

            # 读取整个时间段的 CF 数据
            cf_chunk = ds.variables[cf_varname][t_start:t_end, :, :]

            for i in range(n_stations):
                rlat_idx, rlon_idx = station_cf_indices[i]
                if len(rlat_idx) == 0:
                    continue

                act_year = activation_years[i]
                year_mask = chunk_years >= act_year
                if not np.any(year_mask):
                    continue

                # 向量化提取: (chunk_time, n_pts)
                cf_pts = cf_chunk[:, rlat_idx, rlon_idx]

                with np.errstate(all="ignore"):
                    cf_mean = np.nanmean(cf_pts, axis=1).astype(np.float32)

                cf_mean[~year_mask] = np.nan
                all_nan = np.all(np.isnan(cf_pts), axis=1)
                cf_mean[all_nan] = np.nan

                valid_mask = ~np.isnan(cf_mean)
                power[t_start:t_end, i] = np.where(
                    valid_mask, cf_mean * capacities[i], np.nan
                ).astype(np.float32)

        ds.close()

        # 保存
        self._save_output_nam12(
            power, stations, stype, country_name,
            gcm, realization, rcm, scenario, cf_path,
        )

    # ------------------------------------------------------------------
    # 输出保存
    # ------------------------------------------------------------------

    def _get_output_path(self, stype, region, model, scenario, source,
                         gcm=None, realization=None, rcm=None):
        """构建输出文件路径。"""
        prefix = "pv" if stype == "solar" else "wind"

        if source == "nam12":
            dirname = os.path.join(
                self.output_dir, f"{prefix}_out_NAM-12", gcm, realization
            )
            fname = (
                f"{prefix}_stations_out_NAM-12"
                f"_{gcm}_{realization}_{rcm}_{scenario}_allmonths.nc"
            )
        else:
            dirname = os.path.join(self.output_dir, f"{prefix}_out", model, region)
            fname = f"{prefix}_stations_out_{region}_{model}_{scenario}_allmonths.nc"

        os.makedirs(dirname, exist_ok=True)
        return os.path.join(dirname, fname)

    def _should_skip(self, out_path):
        """判断是否应跳过该输出文件。

        如果 overwrite=False 且文件已存在，则跳过。
        """
        if self.overwrite:
            return False
        if os.path.isfile(out_path):
            logger.info(f"  跳过（文件已存在）: {out_path}")
            return True
        return False

    def _save_output(self, power, stations, stype, region, model,
                     scenario, source, cf_path):
        """保存出力结果为 NetCDF（BCSD/China 格式）。

        如果保存过程中出错，会删除已生成的损坏文件。
        """
        out_path = self._get_output_path(stype, region, model, scenario, source)
        logger.info(f"  保存: {out_path}")

        try:
            # 从 CF 文件读取时间信息
            ds_cf = nc.Dataset(cf_path, "r")
            times = ds_cf.variables["time"][:]
            time_units = ds_cf.variables["time"].units
            ds_cf.close()

            n_time, n_stations = power.shape

            # 创建输出文件
            ds_out = nc.Dataset(out_path, "w", format="NETCDF4")

            # 维度
            ds_out.createDimension("time", n_time)
            ds_out.createDimension("id", n_stations)

            # 时间变量
            time_var = ds_out.createVariable("time", "f8", ("time",))
            time_var.units = time_units
            time_var[:] = times

            # 场站坐标
            lon_var = ds_out.createVariable("station_lon", "f4", ("id",))
            lon_var.units = "degrees_east"
            lon_var[:] = stations["lon"].values.astype(np.float32)

            lat_var = ds_out.createVariable("station_lat", "f4", ("id",))
            lat_var.units = "degrees_north"
            lat_var[:] = stations["lat"].values.astype(np.float32)

            # 场站类型: 0=光伏, 1=风电
            type_var = ds_out.createVariable("station_type", "i1", ("id",))
            type_var[:] = np.where(stations["type"] == "solar", 0, 1).astype(np.int8)

            # 装机容量
            cap_var = ds_out.createVariable("capacity_gw", "f4", ("id",))
            cap_var.units = "GW"
            cap_var[:] = stations["capacity_gw"].values.astype(np.float32)

            # 激活年份
            act_var = ds_out.createVariable("activation_year", "i4", ("id",))
            act_var.units = "year"
            act_var.long_name = "场站激活年份（出力从此年开始有效）"
            act_var[:] = stations["activation_year"].values.astype(np.int32)

            # 出力
            power_var = ds_out.createVariable(
                "power", "f4", ("time", "id"),
                zlib=True, complevel=4, fill_value=np.nan,
            )
            power_var.units = "GW"
            power_var[:] = power

            # 全局属性
            ds_out.region = region
            ds_out.scenario = scenario
            ds_out.source_csv = os.path.basename(self.csv_path)

            ds_out.close()
            logger.info(f"  完成: {out_path}")

        except Exception as e:
            logger.error(f"  保存失败，清理损坏文件: {out_path}\n  错误: {e}")
            if os.path.isfile(out_path):
                os.remove(out_path)
            raise

    def _save_output_nam12(self, power, stations, stype, country_name,
                           gcm, realization, rcm, scenario, cf_path):
        """保存 NAM-12 出力结果为 NetCDF。

        如果保存过程中出错，会删除已生成的损坏文件。
        """
        out_path = self._get_output_path(
            stype, country_name, None, scenario, "nam12",
            gcm=gcm, realization=realization, rcm=rcm,
        )
        logger.info(f"  保存: {out_path}")

        try:
            ds_cf = nc.Dataset(cf_path, "r")
            times = ds_cf.variables["time"][:]
            time_units = ds_cf.variables["time"].units
            ds_cf.close()

            n_time, n_stations = power.shape

            ds_out = nc.Dataset(out_path, "w", format="NETCDF4")
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

            power_var = ds_out.createVariable(
                "power", "f4", ("time", "id"),
                zlib=True, complevel=4, fill_value=np.nan,
            )
            power_var.units = "GW"
            power_var[:] = power

            ds_out.region = country_name
            ds_out.scenario = scenario
            ds_out.source_csv = os.path.basename(self.csv_path)
            ds_out.gcm = gcm
            ds_out.realization = realization
            ds_out.rcm = rcm

            ds_out.close()
            logger.info(f"  完成: {out_path}")

        except Exception as e:
            logger.error(f"  保存失败，清理损坏文件: {out_path}\n  错误: {e}")
            if os.path.isfile(out_path):
                os.remove(out_path)
            raise


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="场站出力计算：基于场站选址和容量因子数据计算逐时出力",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
# BCSD 数据源
python %(prog)s --csv data/stations/stations_SSP1-2.6.csv \\
    --source bcsd --model MIROC-ES2H --scenario ssp126 \\
    --shp data/maps/natural_earth/ne_110m_admin_0_countries.shp

# China 数据源
python %(prog)s --csv data/stations/stations_SSP2-4.5.csv \\
    --source china --model MIROC-ES2H --scenario ssp245

# NAM-12 数据源
python %(prog)s --csv data/stations/stations_SSP5-6.0.csv \\
    --source nam12 --gcm MPI-ESM1-2-LR --realization r1i1p1f1 \\
    --rcm CRCM5 --scenario ssp126""",
    )

    parser.add_argument(
        "--csv", required=True,
        help="场站选址 CSV 文件路径 (stations_SSP*.csv)",
    )
    parser.add_argument(
        "--source", required=True, choices=["bcsd", "china", "nam12"],
        help="CF 数据源类型: bcsd / china / nam12",
    )
    parser.add_argument(
        "--model",
        help="气候模型名 (如 MIROC-ES2H, MPI-ESM1-2-HR, NESM3, CanESM5)",
    )
    parser.add_argument(
        "--scenario",
        help="排放情景代码 (ssp126 / ssp245 / ssp585)；"
             "若不指定则从 CSV 文件名自动推断",
    )
    parser.add_argument(
        "--shp", default="data/maps/natural_earth/ne_110m_admin_0_countries.shp",
        help="国家边界矢量文件路径 (默认: data/maps/natural_earth/ne_110m_admin_0_countries.shp)",
    )
    parser.add_argument(
        "--cfs-dir", default="data/cfs",
        help="CF 数据根目录 (默认: data/cfs)",
    )
    parser.add_argument(
        "--output-dir", default="outputs",
        help="输出根目录 (默认: outputs)",
    )
    # NAM-12 专用参数
    parser.add_argument("--gcm", help="NAM-12 GCM 名 (仅 nam12 需要)")
    parser.add_argument("--realization", help="NAM-12 realization (仅 nam12 需要)")
    parser.add_argument("--rcm", help="NAM-12 RCM 名 (仅 nam12 需要)")
    # BCSD 区域过滤
    parser.add_argument(
        "--region",
        help="BCSD 区域名，仅处理指定国家/区域 (如 Germany, China)；"
             "不指定则处理所有区域",
    )
    # 断点续跑
    parser.add_argument(
        "--overwrite", action="store_true", default=False,
        help="覆盖已有输出文件（默认不覆盖，跳过已存在的文件以支持断点续跑）",
    )

    args = parser.parse_args()

    # 自动推断 scenario
    if args.scenario is None:
        args.scenario = infer_scenario_from_csv(args.csv)
        logger.info(f"自动推断情景: {args.scenario}")

    # 检查 model 参数（非 nam12 时必需）
    if args.source != "nam12" and args.model is None:
        parser.error("非 nam12 数据源需要指定 --model")

    return args


def main():
    args = parse_args()

    calc = StationOutputCalculator(
        csv_path=args.csv,
        source=args.source,
        model=args.model,
        scenario=args.scenario,
        shp_path=args.shp,
        cfs_dir=args.cfs_dir,
        output_dir=args.output_dir,
        gcm=args.gcm,
        realization=args.realization,
        rcm=args.rcm,
        region=args.region,
        overwrite=args.overwrite,
    )
    calc.run()
    logger.info("全部完成。")


if __name__ == "__main__":
    main()
