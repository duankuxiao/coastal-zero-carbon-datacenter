<p align="center">
  <img src="figures/logo.png" alt="Logo" width="180"/>
</p>

# Coastal Zero-Carbon Datacenter

沿海数据中心零碳潜力评估工具包。项目用于比较空气源冷却与海水源冷却在不同沿海城市的数据中心能耗、PUE、碳排放表现，并结合 2025 年 ERA5 海上风电、海表温度、EPW 气象、电网碳强度和数据中心工作负载，评估年度绿电覆盖与小时级负荷转移调度效果。

当前主流程以国家 2030 数据中心容量增长情景为入口：读取国家、城市和 small / medium / large 数据中心规模配置，把每个国家的沿海新增容量分配到代表城市和设施规模，再批量运行冷却系统对比与零碳调度计算。

## 功能概览

- 单城市数据中心 IT、冷却、总能耗、PUE 和碳排放计算。
- 空气源冷却与海水源冷却对比。
- 按设施容量自动估算物理 rack 数量，避免 medium / large 设施被压缩到固定 rack 数导致不合理高出口温度。
- 海水冷却按设施容量和逐小时热排放自动估算取排水流量、换热单元数量和换热器 UA。
- 基于 timestamp 对齐 workload、EPW 气温、SST 和电网碳强度。
- 基于 ERA5 海上风电数据估算年度电量覆盖所需风电装机容量。
- 基于线性规划优化小时级风电、负荷转移、储能和电网购电调度。
- 支持 `min-grid-mwh` 和 `min-grid-co2` 两类优化目标。
- 支持配置文件驱动的国家-城市-规模批量计算。

## 目录结构

```text
.
├── run.py                                      # 当前主批量入口，读取配置并运行国家增长情景
├── scripts/
│   └── run_config.txt                         # run.py 默认配置文件，JSON 格式
├── energy/
│   ├── calculate_datacenter_energy.py         # 单城市能耗/排放计算入口
│   ├── datacenter.py                          # 数据中心 IT 与 HVAC 详细模型
│   └── seawater_heat_pump.py                  # 海水源热泵、取排水、换热器和控制模型
├── renewables/
│   ├── calculate_wind_capacity.py             # 年度风电装机容量估算入口
│   └── wind_power.py                          # ERA5 风电发电量计算基础函数
├── optimization/
│   └── optimize_zero_carbon.py                # 单城市小时级风电/储能/负荷转移/购电优化器
├── data/
│   ├── coastal_datacenter_city_manifest.xlsx  # 国家、城市和数据中心规模清单
│   ├── Workload/                              # CPU 工作负载曲线
│   ├── epw_download_toolkit/                  # ERA5-only EPW 生成脚本与气象文件
│   ├── sst_download_toolkit/                  # Open-Meteo 海表温度采集脚本与数据
│   ├── ci_download_toolkit/                   # Electricity Maps 电网碳强度脚本与数据
│   └── offshore_wind_download_toolkit/        # ERA5 海上风电输入数据与下载清单
├── tests/                                     # 单元测试
├── utils/                                     # 配置读取、输出表和辅助工具
├── figures/                                  # 项目图片
└── results/                                  # 默认结果输出目录，已被 .gitignore 忽略
```

## 数据说明

主要输入数据包括：

- `data/coastal_datacenter_city_manifest.xlsx`：核心清单文件。默认读取三个 sheet：
  - `Country_manifest`：国家 2025 基准容量、2030 情景容量和沿海占比。
  - `City_manifest`：国家、代表城市、EPW 文件前缀和 `toolkit_ready` 标记。
  - `Datacenter_scale`：small / medium / large 的容量比例和单体容量上下限。
- `data/Workload/*.csv`：小时级 CPU 负载曲线，默认使用 `GoogleClusteData_CPU_Data_Hourly_1.csv`，要求包含 `cpu_load` 列。
- `data/epw_download_toolkit/epw_2025_era5_only/`：解压后的 2025 ERA5-only AMY EPW 文件目录。仓库同时包含 `epw_2025_era5_only.zip`，模型读取解压目录。
- `data/sst_download_toolkit/sea_surface_temperature_2025_openmeteo.csv`：小时级海表温度。
- `data/ci_download_toolkit/city_grid_carbon_intensity_electricitymaps_10y.csv`：小时级电网碳强度宽表。
- `data/offshore_wind_download_toolkit/strict_coastal_download_manifest.csv`：城市与海上风电代表点、ERA5 文件的对应关系。
- `data/offshore_wind_download_toolkit/offshore_wind/*.nc`：沿海城市代表海点的 ERA5 海上风电气象输入。

## 环境依赖

要求 Python 3.10 或更高版本。建议在虚拟环境中安装依赖：

```bash
python -m pip install -r requirements.txt
```

重新生成 ERA5 EPW 或海上风电数据时，需要配置 Copernicus CDS API 凭据。下载 Electricity Maps 数据时，需要设置 API Token。

## 快速开始

列出可用城市：

```bash
python -m energy.calculate_datacenter_energy --list-cities
```

计算单城市空气源冷却结果：

```bash
python -m energy.calculate_datacenter_energy --city "Shanghai" --cooling air_source --rated-it-power-kw 20000 --hours 8760 --time-alignment latest
```

计算单城市海水源冷却结果：

```bash
python -m energy.calculate_datacenter_energy --city "Shanghai" --cooling seawater --rated-it-power-kw 20000 --hours 8760 --time-alignment sst --json
```

估算单城市年度电量覆盖所需海上风电装机容量：

```bash
python -m renewables.calculate_wind_capacity --city "Shanghai" --cooling seawater --rated-it-power-kw 20000 --hours 8760 --json
```

运行单城市小时级零碳调度示例：

```bash
python -m optimization.optimize_zero_carbon
```

该入口当前使用模块内的 Shanghai 示例参数。如需批量运行国家增长情景，使用根目录 `run.py`。

## 批量运行

默认配置位于 `scripts/run_config.txt`。该文件是 JSON 格式，控制输入文件、输出目录、仿真时段、风机参数、负荷转移比例、并发数和要运行的 case。

默认配置示例包括：

- `air_source_baseline`：空气源冷却基准。
- `seawater_baseline`：海水源冷却基准。
- `seawater_load_shift_co2`：海水源冷却 + 负荷转移，以 `min-grid-co2` 为优化目标。

只生成国家增长和城市规模分配，不调用能耗、风电或优化模型：

```bash
python run.py --dry-run --output-dir results
```

快速验证少量国家和较短时段：

```bash
python run.py --max-countries 1 --hours 24 --workers 2 --output-dir results/smoke
```

完整运行默认配置：

```bash
python run.py --config-file scripts/run_config.txt
```

常用覆盖参数：

```bash
python run.py --countries China Japan --hours 8760 --start-time "2025-01-01 00:00" --workers 2 --output-dir results/country_growth
```

`run.py` 的主要命令行参数：

- `--config-file`：配置文件路径，默认 `scripts/run_config.txt`。
- `--manifest-file`：覆盖配置中的清单文件路径。
- `--output-dir`：覆盖配置中的结果目录。
- `--workload-file`：覆盖配置中的工作负载文件。
- `--include-not-ready`：包含 `toolkit_ready` 不为 true 的城市。
- `--dry-run`：只写入基础分配表，不运行模型。
- `--countries`：限制运行一个或多个国家。
- `--max-countries`：只运行筛选后的前 N 个国家，适合快速验证。
- `--write-debug-scale-results`：额外输出 scale-level 中间结果。
- `--idle-power-fraction`、`--sst-fraction`、`--load-shift-fraction`、`--wind-loss-fraction`：敏感性分析参数。
- `--workers`：并发 worker 数。默认配置为 2，可按机器资源调整。

配置文件中的 `cases` 控制运行内容。case 字段包括：

- `cooling_type`：`air_source` 或 `seawater`。
- `optimization_enabled`：`false` 表示只做冷却/风机容量基准，`true` 表示运行负荷转移优化。
- `optimization_method`：当前支持 `baseline` 和 `load_shift`。
- `optimization_objective` 或 `optimization_objectives`：支持 `co2` / `min-grid-co2` 和 `mwh` / `min-grid-mwh`。

## 输出文件

`run.py --dry-run` 输出：

```text
country_growths.csv
city_scale_allocations.csv
```

冷却系统基准输出使用统一最终表结构：

```text
city_air_source_baseline_<hours>.csv
country_air_source_baseline_<hours>.csv
city_seawater_baseline_<hours>.csv
country_seawater_baseline_<hours>.csv
```

负荷转移优化输出按冷却类型和目标命名，例如：

```text
city_seawater_load_shift_co2_<hours>.csv
country_seawater_load_shift_co2_<hours>.csv
city_seawater_load_shift_mwh_<hours>.csv
country_seawater_load_shift_mwh_<hours>.csv
```

加 `--write-debug-scale-results` 时会额外输出：

```text
debug_cooling_scale_<hours>.csv
debug_load_shift_scale_<hours>.csv
```

最终表的核心字段包括：

- `country`、`city`、`growth_scenario`
- `coastal_datacenter_growth_capacity_mw`
- `cooling_energy_kwh`、`server_energy_kwh`、`total_energy_kwh`
- `cooling_carbon_emissions_kgco2`、`server_carbon_emissions_kgco2`、`total_carbon_emissions_kgco2`
- `required_wind_capacity_mw`、`wind_annual_generation_mwh`
- `wind_curtailment_mwh`、`renewable_physical_coverage_fraction`
- `grid_purchase_mwh`、`grid_purchase_co2_kg`

单城市模块会在 `results/` 下写入各自的 CSV：

- `datacenter_energy_<city>_<cooling_type>_<rated_power>.csv`
- `wind_capacity_<city>_<cooling_type>_<rated_power>.csv`
- `optimization_<city>_<cooling>_<objective>_summary.csv`
- `optimization_<city>_<cooling>_<objective>_hourly_inputs.csv`
- `optimization_<city>_<cooling>_<objective>_hourly_dispatch.csv`

## 模型说明

### 能耗与冷却

`energy.calculate_datacenter_energy` 将 workload、EPW 干球温度、海表温度和电网碳强度对齐到同一小时序列，然后调用详细数据中心模型计算 IT 能耗、冷却能耗、总能耗和碳排放。

IT/rack 模型会根据 `rated_it_power_kw` 自动估算物理 rack 数量。默认目标功率密度为 `50 kW/rack`。为了避免 large 设施展开成过多对象，仿真仍使用代表 rack，并通过权重汇总 CPU 功率、IT 风扇功率和 CRAC 回风温度。

海水源热泵模型包含冷冻水回路、海水取排水回路、板式换热器、热泵机组和逐小时控制逻辑。配置允许自动放大海水流量、换热单元数和热泵容量，也保留固定容量不足时的约束违规与告警输出。

### 时间对齐

`--hours` 表示仿真小时数。碳强度、SST 和 EPW 不按简单行号截取，而是按 timestamp 或 day-of-year/hour 对齐：

- `seawater` 默认使用 `sst` 时间轴。
- `air_source` 默认使用最新可用碳强度窗口。
- 指定 `--start-time` 时使用从指定时间开始的窗口。
- 碳强度短缺口默认最多插值 6 小时，可通过 `--max-carbon-gap-hours` 调整。

### 海上风电

`renewables.calculate_wind_capacity` 做年度电量平衡：

```text
required_wind_capacity_mw = datacenter_total_energy_mwh / wind_generation_per_mw_mwh
```

`wind_generation_per_mw_mwh` 来自 ERA5 小时级风速、温度和气压数据，以及通用海上风机功率曲线参数。该估算用于年度装机容量，不代表小时级 24/7 零碳匹配。

### 小时级调度

`optimization.optimize_zero_carbon` 使用确定性线性规划模型协调小时级数据中心需求、固定风电出力、负荷转移、储能和电网购电。

- `min-grid-mwh`：最小化全年电网购电量。
- `min-grid-co2`：最小化全年购电碳排放。
- 负荷转移保持全年总需求不变，并限制每小时上下浮动比例。
- 储能采用循环 SOC 约束，支持容量、充放电功率和往返效率参数。
- 当前 `run.py` 批量主结果只写出 baseline 与 load-shift，单城市优化函数仍支持储能参数。

## 数据再生成

海表温度：

```bash
cd data/sst_download_toolkit
python collect_sst_openmeteo_quick.py --year 2025 --output sea_surface_temperature_2025_openmeteo.csv
```

电网碳强度：

```powershell
$env:ELECTRICITYMAPS_TOKEN = "your_token"
python data/ci_download_toolkit/download_electricitymaps_10y_hourly.py --output-dir data/ci_download_toolkit --output-wide city_grid_carbon_intensity_electricitymaps_10y.csv --end now --years-back 10
```

EPW 气象文件：

```bash
cd data/epw_download_toolkit
python batch_generate_epw_era5_only_global.py --year 2025 --out-dir epw_2025_era5_only --zip-output epw_2025_era5_only.zip
```

海上风电 ERA5 输入：

```bash
python data/offshore_wind_download_toolkit/download_era5_strict_coastal_wind_inputs.py
```

各下载工具的完整参数请参考对应目录下的 `README.md`。

## 测试

运行单元测试：

```bash
python -m pytest -q
```

重点测试覆盖：

- `tests/test_datacenter_rack_sizing.py`：rack 自动定容和 large 海水设施自动扩容。
- `tests/test_seawater_heat_pump.py`：海水热泵自动定容、取排水自动扩流和固定容量违规报告。
- `tests/test_time_alignment.py`：SST、碳强度、EPW 和自定义起始时间对齐。
- `tests/test_zero_carbon_optimizer.py`：购电量/购电碳排放目标、储能和负荷转移约束。
- `tests/test_country_growth_allocation.py`：国家增长容量、城市规模分配、国家平均和批量输出逻辑。

## 注意事项

- `results/`、解压后的 EPW 目录和下载缓存默认不纳入 Git。
- 批量运行中缺少有效碳强度、SST、EPW 或风电输入的城市会失败或跳过，并在输出行中保留 `status` 和 `error_message`。
- 年度绿电覆盖不等同于小时级 24/7 零碳。小时级匹配需要结合风电时序、负荷转移、储能和购电约束解释。
- 仓库包含较大的 CSV、EPW 和 ERA5 数据文件。公开发布前应确认数据授权、引用方式和文件体积是否符合需求。
