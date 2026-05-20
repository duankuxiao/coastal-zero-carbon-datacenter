# EPW 2025 ERA5-only 生成工具包

本工具包用于为项目城市生成统一年份的 EPW 气象输入。当前版本不再使用 Climate.OneBuilding TMYx 典型气象年文件，而是基于 ERA5 小时级再分析数据生成 2025 年 Actual Meteorological Year (AMY) EPW 文件，便于与 2025 年海表温度、碳强度和负载时间窗口做一致对比。

ERA5-only 方案使用全球可用的 ERA5 变量生成温度、湿度、气压、风速、云量、降水和太阳辐射等字段，避免 CAMS Radiation Service 卫星视场限制导致部分美洲、印度和智利城市无法生成辐射数据的问题。

## 文件说明

- `batch_generate_epw_era5_only_global.py`：批量下载 ERA5 数据并生成 2025 年 AMY EPW 文件；支持 CDS 返回 ZIP、异常文本文件检测、坏缓存重试，以及时间序列和网格型 ERA5 NetCDF 两种输入。
- `target_city_map_epw_coordinates_checked.csv`：220 个城市/都市圈的 EPW 生成坐标、生成方法建议、CAMS FOV 判定和目标 EPW 文件名。
- `cams_fov_problem_cities.csv`：CAMS 辐射视场可能失败的城市清单，用于记录为什么改用 ERA5-only 全局生成方法。
- `epw_2025_era5_only/`：当前随附的 2025 年 ERA5 AMY EPW 文件目录。
- `epw_2025_era5_only.zip`：包含当前已生成 EPW 文件和 `epw_era5_only_status.csv` 状态表的压缩包。
- `README.md`：本说明。

当前 manifest 覆盖 220 个城市/都市圈；本目录下当前工作区的 `epw_2025_era5_only/` 和 zip 包包含 209 个已生成 EPW 文件。缺失城市可按下方命令继续生成或重跑。

## 环境依赖

生成 EPW 需要 Python 3.12 或兼容环境，并需要配置 Copernicus CDS API 凭据。

```bash
pip install cdsapi xarray netCDF4 h5netcdf pandas numpy pvlib timezonefinder tqdm
```

## 使用方式

在本目录运行：

```bash
python batch_generate_epw_era5_only_global.py ^
  --input target_city_map_epw_coordinates_checked.csv ^
  --year 2025 ^
  --out-dir epw_2025_era5_only ^
  --cache-dir era5_only_cache ^
  --status-csv epw_era5_only_status.csv ^
  --zip-output epw_2025_era5_only.zip
```

常用选项：

- `--limit N`：只生成前 N 个城市，便于测试。
- `--overwrite`：覆盖已有 EPW 和缓存文件。
- `--fallback-area`：ERA5 time-series 请求失败时，退回按月下载城市附近小范围网格数据。
- `--apply-time-zone-to-data`：按标准 UTC offset 平移数据时间戳；默认保留 UTC 小时序列。

## 运行后输出

- `epw_2025_era5_only/*.epw`：按 `EPW filename` 命名的 2025 年 AMY EPW 文件，格式为 `序号_国家_城市_2025_AMY_ERA5.epw`。
- `epw_era5_only_status.csv`：逐城市下载、生成、校验状态，以及实际 EPW 路径、缓存路径和异常信息。
- `epw_2025_era5_only.zip`：包含已生成 EPW 文件和状态表的压缩包。

核心能耗计算代码读取 `epw_2025_era5_only/` 目录中的 `.epw` 文件；如果工作区中只有 `epw_2025_era5_only.zip`，请先解压到同名目录后再运行仿真。

## 校验规则

脚本会检查：

1. EPW 数据行是否为 8760 行；
2. 是否存在 2 月 29 日；
3. 第 7 列（索引 6）室外干球温度是否可解析为数值，单位 degC；
4. 第 9 列（索引 8）相对湿度是否可解析为数值，单位 %；
5. 第 10 列（索引 9）气压是否可解析为数值，单位 Pa；
6. 干球温度、相对湿度和气压是否落在宽松的合理范围内。

## 数据生成方法

- 坐标优先使用 `target_city_map_epw_coordinates_checked.csv` 中的 `EPW latitude` / `EPW longitude`。
- 太阳辐射来自 ERA5 的 `surface_solar_radiation_downwards` 和 `total_sky_direct_solar_radiation_at_surface`，单位从 J/m2 转为 Wh/m2。
- DNI 由 ERA5 直接水平辐射和太阳天顶角估算得到；该近似主要用于保证全球城市可生成一致气象输入。
- EPW header 中记录 `ERA5 (ECMWF)`、2025 年数据周期和 ERA5-only 生成说明。
- 默认生成非闰年 8760 小时数据，与项目中 2025 年 SST 和年度仿真窗口一致。

## 注意事项

- ERA5-only EPW 是实际气象年数据，不是 TMYx 典型气象年。用于跨城市对比时，所有城市使用同一目标年份 2025，时间基准更一致。
- CAMS FOV 问题由辐射产品覆盖范围引起，不应通过把城市坐标强行移动到远处卫星视场内解决；本工具保留城市坐标并改用 ERA5 全球辐射变量。
- `era5_only_cache/` 是本地下载缓存，可在需要重新下载时清理或配合 `--overwrite` 使用。
