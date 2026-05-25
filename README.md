<p align="center">
  <img src="figures/logo.png" alt="Logo" width="200"/>
</p>

# Coastal Zero-Carbon Datacenter

沿海数据中心零碳潜力评估工具包，用于比较常规空气源冷却与海水源冷却在不同城市中的能耗、PUE、碳排放表现，并进一步结合 2025 年 ERA5 海上风电数据评估年度绿电覆盖和小时级零碳调度潜力。

项目整合全球城市/都市圈清单、2025 年 ERA5-only AMY EPW 气象文件、Open-Meteo 海表温度、Electricity Maps 电网碳强度、ERA5 海上风电气象数据和数据中心工作负载曲线，支撑“沿海数据中心 + 海水冷却 + 海上风电 + 储能/负荷转移”的年度和小时级分析。

## 功能概览

- 单城市数据中心能耗、PUE 和碳排放计算。
- 空气源冷却与海水源冷却的沿海城市批量对比。
- 基于 timestamp 对齐 workload、EPW 气温、SST 和电网碳强度。
- 气象输入统一为 2025 年 ERA5 AMY EPW，避免典型气象年和实际年份混用造成的对比偏差。
- 基于 ERA5 海上风电数据估算年度电量平衡所需的风电装机容量。
- 基于线性规划优化小时级风电、储能、负荷转移和电网购电调度。
- 支持 `min-grid-mwh` 和 `min-grid-co2` 两类优化目标。
- 提供 EPW、海表温度、电网碳强度和海上风电输入数据的下载与再生成脚本。

## 目录结构

```text
.
├── scripts/
│   ├── run_baseline.py                        # 沿海城市空气源/海水源批量基准测试
│   ├── run_optimize.py                        # 沿海城市批量零碳调度优化
│   └── __init__.py
├── energy/
│   ├── calculate_datacenter_energy.py         # 单城市数据中心能耗/排放计算入口
│   ├── datacenter.py                          # 数据中心 IT 与 HVAC 详细模型
│   └── seawater_heat_pump.py                  # 海水源热泵、取排水、换热器和控制模型
├── renewables/
│   ├── calculate_wind_capacity.py             # 年度风电装机容量估算入口
│   └── wind_power.py                          # ERA5 风电发电量计算基础函数
├── optimization/
│   ├── optimize_zero_carbon.py                # 单城市风电/储能/负荷转移/购电优化器
│   ├── battery_model.py                       # 储能模型
│   ├── battery_env_fwd_view.py                # 储能强化学习环境
│   └── load_shift.py                          # 可转移负荷环境
├── data/
│   ├── target_city_map.csv                    # 220 个城市/都市圈及沿海分类
│   ├── Workload/                              # CPU 工作负载曲线
│   ├── epw_download_toolkit/                  # 2025 ERA5-only EPW 生成脚本与气象文件
│   ├── sst_download_toolkit/                  # 海表温度采集脚本与数据
│   ├── ci_download_toolkit/                   # 电网碳强度采集脚本与数据
│   └── offshore_wind_download_toolkit/        # ERA5 海上风电输入数据与下载清单
├── docs/                                      # 方法说明与案例文档
├── tests/                                     # 单元测试
├── utils/                                     # 配置读取和辅助工具
├── harl/                                      # 强化学习算法与环境代码
├── figures/                                  # 图表和项目图片
└── results/                                  # 计算结果输出目录
```

所有批量运行入口统一放在 `scripts/` 中，推荐使用 `python -m scripts.run_baseline` 和 `python -m scripts.run_optimize`。

## 数据说明

主要输入数据包括：

- `data/target_city_map.csv`：城市/都市圈、区域、经纬度和沿海分类。
- `data/Workload/*.csv`：小时级 CPU 负载曲线，要求包含 `cpu_load` 列，取值通常为 0 到 1。
- `data/epw_download_toolkit/target_city_map_epw_coordinates_checked.csv`：EPW 生成坐标、CAMS FOV 判定和目标 EPW 文件名。
- `data/epw_download_toolkit/epw_2025_era5_only/*.epw`：2025 年 ERA5-only AMY EPW 气象文件。核心模型读取解压后的 EPW 目录，不直接读取 zip 包。
- `data/sst_download_toolkit/sea_surface_temperature_2025_openmeteo.csv`：非 Inland 城市的小时级海表温度，单位为 degC。
- `data/ci_download_toolkit/carbon_intensity_electricitymaps.csv`：小时级电网碳强度宽表，单位为 gCO2eq/kWh。
- `data/offshore_wind_download_toolkit/offshore_wind/*.nc`：沿海城市代表海点的 ERA5 风电气象输入。
- `data/offshore_wind_download_toolkit/strict_coastal_offshore_wind_points_manifest.csv`：城市与海上风电代表点、ERA5 网格点的对应关系。

各类下载和再生成脚本位于对应的 `*_download_toolkit` 目录中。

## 环境依赖

基础计算依赖：

```bash
pip install numpy pandas
```

海上风电 `.nc`/netCDF 数据读取依赖：

```bash
pip install xarray netCDF4
```

小时级零碳调度优化依赖：

```bash
pip install scipy
```

重新生成 2025 年 ERA5-only EPW 文件时，还需要配置 Copernicus CDS API 凭据，并安装：

```bash
pip install cdsapi xarray netCDF4 h5netcdf pvlib timezonefinder tqdm
```

Electricity Maps 数据下载需要设置 API Token。

## 快速开始

列出可用城市：

```bash
python -m energy.calculate_datacenter_energy --list-cities
```

计算单城市空气源冷却结果：

```bash
python -m energy.calculate_datacenter_energy ^
  --city "Shanghai" ^
  --cooling air_source ^
  --rated-it-power-kw 20000 ^
  --hours 8760 ^
  --time-alignment latest
```

计算单城市海水源冷却结果：

```bash
python -m energy.calculate_datacenter_energy ^
  --city "Shanghai" ^
  --cooling seawater ^
  --rated-it-power-kw 20000 ^
  --hours 8760 ^
  --time-alignment sst ^
  --json
```

估算单城市年度电量平衡所需的海上风电装机容量：

```bash
python -m renewables.calculate_wind_capacity ^
  --city "Shanghai" ^
  --cooling seawater ^
  --rated-it-power-kw 20000 ^
  --hours 8760 ^
  --json
```

运行沿海城市空气源/海水源批量基准测试：

```bash
python -m scripts.run_baseline ^
  --rated-it-power-kw 20000 ^
  --idle-power-fraction 0.3 ^
  --hours 8760 ^
  --start-time "2025-01-01 00:00" ^
  --time-alignment sst ^
  --max-carbon-gap-hours 6 ^
  --output-dir results
```

运行所有沿海城市批量零碳调度优化前，先在 `scripts/run_optimize.py` 的 `main()` 中修改函数输入参数，例如：

```python
_, _, output_files = run_optimizations(
    cooling="seawater",
    objectives=("min-grid-mwh", "min-grid-co2"),
    rated_it_power_kw=20000.0,
    battery_capacity_mwh=535.4,
    battery_roundtrip_efficiency=0.97,
    grid_import_limit_mw=None,
    battery_charge_limit_mw=25.0,
    battery_discharge_limit_mw=25.0,
    load_shift_fraction=0.3,
    output_dir=DEFAULT_OUTPUT_DIR,
)
```

然后运行：

```bash
python -m scripts.run_optimize
```

单城市优化也可以在 Python 中直接调用：

```python
from optimization.optimize_zero_carbon import optimization

result = optimization(
    city="Shanghai",
    cooling="seawater",
    wind_capacity_mw=75.21,
    wind_nc_file="data/offshore_wind_download_toolkit/offshore_wind/OW_006_China_Shanghai_era5_atmos_2025-01-01_2025-12-31.nc",
    objective="min-grid-co2",
    include_hourly=False,
    output_results=True,
)
```

## 输出结果

单城市数据中心能耗计算会在 `results/` 下生成：

```text
datacenter_energy_<city>_<cooling_type>_<rated_power>.csv
```

单城市风电装机容量估算会生成：

```text
wind_capacity_<city>_<cooling_type>_<rated_power>.csv
```

沿海城市基准测试会生成三张表：

```text
baseline_air_source_results_<rated_power>_<hours>.csv
baseline_seawater_results_<rated_power>_<hours>.csv
baseline_summary_<rated_power>_<hours>.csv
```

沿海城市批量零碳调度优化会生成三张表，不保存每个城市的 8760 小时明细：

```text
strict_coastal_optimization_city_results_<cooling>_<hours>.csv
strict_coastal_optimization_summary_<cooling>_<hours>.csv
strict_coastal_optimization_country_summary_<cooling>_<hours>.csv
```

城市结果表每行对应一个城市、策略和优化目标，包含年度需求、风电装机、风电文件、电网购电量、购电碳排放、可再生物理覆盖率、弃风、储能充放电和负荷转移量。汇总表按 `objective + scenario` 聚合全部城市，并计算相对 `baseline` 的能耗、碳排放和购电量节省。国家汇总表按 `country_area + objective + scenario` 聚合，使用同一套相对 `baseline` 的优化效果指标。

## 模型说明

### 能耗与冷却模型

`energy.calculate_datacenter_energy` 将 workload、城市气象、海表温度和电网碳强度对齐到同一小时序列，然后调用详细数据中心模型计算 IT 能耗、冷却能耗、总能耗和碳排放。

海水源热泵模型包括：

- 冷冻水回路：根据供回水温差、比热和泵效率计算冷冻水流量与泵功耗。
- 海水取排水回路：根据允许温升、管线长度、管径、粗糙度、局部损失和泵效率计算海水流量、压降和泵功耗。
- 板式换热器：基于 UA、有效度、NTU 和夹点温差判断自然冷却能力。
- 热泵机组：优先使用配置中的性能曲线，缺失曲线时使用 Carnot 近似回退。
- 控制逻辑：逐小时判断自然冷却、混合冷却或机械热泵模式。

### 时间对齐

`--hours` 表示仿真小时数。碳强度不再简单读取 CSV 前 `hours` 行，而是按目标 timestamp 对齐：

- `seawater` 默认使用 `sst`，以 SST 文件 timestamp 作为主时间轴。
- `air_source` 默认使用 `latest`，使用碳强度文件中最新的可用时间窗口。
- `scripts.run_baseline` 默认使用 `sst`，保证空气源与海水源使用同一时期碳排放因子。
- 指定 `--start-time "2025-01-01 00:00"` 时，可切换到从指定时间开始截取 `hours` 小时。
- 碳强度缺少少量小时会按时间插值，默认最大连续缺口为 6 小时，可通过 `--max-carbon-gap-hours` 调整。
- EPW 干球温度按 2025 年 AMY EPW 的 8760 小时读取，并按目标 timestamp 的 day-of-year/hour 映射到仿真时间轴。

### 海上风电装机容量

`renewables.calculate_wind_capacity` 做年度电量匹配：

```text
required_wind_capacity_mw = datacenter_total_energy_mwh / wind_generation_per_mw_mwh
```

其中 `wind_generation_per_mw_mwh` 来自 ERA5 小时级风速、温度和气压数据。该模型用于年度装机估算，不模拟小时级供需平衡、储能、弃风或 24/7 碳匹配。

### 小时级零碳调度优化

`optimization.optimize_zero_carbon` 使用确定性线性规划模型协调小时级数据中心负荷、固定风电出力、储能、负荷转移和电网购电。

- `min-grid-mwh`：最小化全年电网购电量。
- `min-grid-co2`：最小化全年电网购电碳排放。
- 储能采用循环 SOC 约束，支持容量、充电功率、放电功率和往返效率参数。
- 可转移负荷按每小时上下浮动比例约束，并保持全年总需求不变。
- 弃风作为未使用风电输出，不默认计入外送收益或减排收益。

## 数据再生成

海表温度：

```bash
python data/sst_download_toolkit/collect_sst_openmeteo_quick.py ^
  --input data/target_city_map.csv ^
  --year 2025 ^
  --output data/sst_download_toolkit/sea_surface_temperature_2025_openmeteo.csv
```

电网碳强度：

```bash
set ELECTRICITYMAPS_TOKEN=your_token

python data/ci_download_toolkit/download_electricitymaps_10y_hourly.py ^
  --manifest city_electricitymaps_request_manifest.csv ^
  --output-dir data/ci_download_toolkit ^
  --output-wide carbon_intensity_electricitymaps.csv ^
  --end now ^
  --years-back 10 ^
  --emission-factor-type direct ^
  --unavailable-zone-action missing
```

EPW 气象文件：

```bash
cd data/epw_download_toolkit
python batch_generate_epw_era5_only_global.py ^
  --input target_city_map_epw_coordinates_checked.csv ^
  --year 2025 ^
  --out-dir epw_2025_era5_only ^
  --cache-dir era5_only_cache ^
  --status-csv epw_era5_only_status.csv ^
  --zip-output epw_2025_era5_only.zip
```

海上风电 ERA5 输入：

```bash
python data/offshore_wind_download_toolkit/download_era5_strict_coastal_wind_inputs.py
```

## 注意事项

- 批量 baseline 中缺少有效碳强度、SST 或风电输入数据的城市会被跳过，并在命令行输出跳过原因。
- 批量优化默认不保存每个城市的 8760 小时明细，避免输出文件过多；如需单城市小时级结果，请调用 `run_optimization(..., output_results=True, include_hourly=True)`。

### `scripts/run_optimize.py` 结果字段说明

批量优化会在汇总表中按 `objective` 和 `scenario` 对比三种场景：

- `baseline`：不启用负荷调整，不启用蓄电池。该场景用于给出未优化时的数据中心能耗、碳排放、风电覆盖量、弃风量、风电覆盖率和购电量。
- `load_shift`：只启用负荷调整，不启用蓄电池。汇总表会同时给出相对 `baseline` 的能耗、碳排放和购电量节约值及节约比例。
- `load_shift_battery`：同时启用负荷调整和蓄电池。汇总表会给出所需蓄电池容量，并给出相对 `baseline` 的能耗、碳排放和购电量节约值及节约比例。

关键结果列含义如下：

- `datacenter_total_energy_mwh` / `annual_demand_mwh`：数据中心年度总用电需求，单位 MWh。负荷调整只改变小时分布，全年需求保持不变。
- `grid_purchase_mwh`：从电网购电量，单位 MWh。
- `grid_purchase_co2_kg`：购电对应碳排放，单位 kg CO2。
- `annual_wind_mwh`：配置风电容量对应的年度风电发电量，单位 MWh。
- `wind_coverage_mwh`：由风电覆盖的数据中心用电量，按 `annual_demand_mwh - grid_purchase_mwh` 计算，单位 MWh。
- `wind_curtailment_mwh`：弃风量，单位 MWh。
- `renewable_physical_coverage_fraction`：风电覆盖率，按 `wind_coverage_mwh / annual_demand_mwh` 计算。
- `battery_configured_capacity_mwh`：该场景配置给优化器的蓄电池容量，单位 MWh。
- `battery_required_capacity_mwh`：优化结果实际用到的蓄电池能量容量，按蓄电池 SOC 最大值与最小值之差计算，单位 MWh。
- `energy_savings_mwh_vs_baseline` / `energy_savings_pct_vs_baseline`：相对 `baseline` 的数据中心能耗节约值和比例。
- `co2_savings_kg_vs_baseline` / `co2_savings_pct_vs_baseline`：相对 `baseline` 的碳排放节约值和比例。
- `grid_purchase_savings_mwh_vs_baseline` / `grid_purchase_savings_pct_vs_baseline`：相对 `baseline` 的购电量节约值和比例。
- 年度绿电覆盖不等同于小时级 24/7 零碳。小时级匹配需要结合风电时序、储能、购电约束和负荷转移约束解释。
- 仓库包含较大的 CSV、EPW 和 ERA5 数据文件，公开发布前应确认数据授权、引用方式和文件体积是否符合需求。
