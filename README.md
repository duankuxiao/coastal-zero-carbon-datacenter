<p align="center">
  <img src="figures/logo.png" alt="Logo" width="200"/>
</p>

# Coastal Zero-Carbon Datacenter

沿海数据中心零碳潜力评估工具包，用于比较常规空气源冷却与海水源冷却在不同城市中的能耗、PUE、碳排放表现，并进一步估算满足数据中心年度用电量所需的海上风电装机容量。

项目结合全球城市/都市圈清单、EPW 气象数据、Open-Meteo 海表温度、Electricity Maps 电网碳强度、ERA5 海上风电气象数据和数据中心工作负载曲线，支撑“沿海数据中心 + 海水冷却 + 海上风电/绿电”的年度能耗和减排潜力分析。

## 功能概览

- 单城市数据中心能耗和碳排放计算。
- 空气源/常规冷却与海水源冷却对比。
- 严格沿海城市批量基准测试。
- 基于 timestamp 将碳强度与 SST、EPW 温度、workload 自动对齐。
- baseline 对比默认使用 SST 时间窗口，确保空气源和海水源使用同一时期碳排放因子。
- 详细海水源热泵模型，覆盖自然冷却、混合冷却和机械热泵三种运行模式。
- 输出服务器/冷却系统分项能耗与分项碳排放。
- 基于 ERA5 海上风电数据估算满足年度总电耗所需的风电装机容量。
- 提供 EPW、海表温度、碳强度和海上风电输入数据下载与校验脚本。

## 目录结构

```text
.
├── run_baseline.py                           # 严格沿海城市批量对比与风电装机估算入口
├── coastal_data_center_outline_v2.md          # 研究框架与论文提纲
├── core/
│   ├── calculate_datacenter_energy.py         # 单城市能耗/排放计算入口
│   ├── calculate_wind_capacity.py             # 年度风电装机容量估算入口
│   ├── wind_power.py                          # ERA5 风电发电量计算基础函数
│   ├── datacenter.py                          # 数据中心 IT 与 HVAC 详细模型
│   └── seawater_heat_pump.py                  # 海水源热泵、取排水、换热器和控制模型
├── utils/
│   ├── dc_config.json                         # 当前默认数据中心配置
│   ├── dc_config_reader.py                    # JSON 配置读取器
│   └── dc_config*.json                        # 其他数据中心配置样例
├── data/
│   ├── target_city_map.csv                    # 220 个城市/都市圈及沿海分类
│   ├── Workload/                              # CPU 工作负载曲线
│   ├── epw_download_toolkit/                  # EPW 下载、校验脚本与气象文件
│   ├── sst_download_toolkit/                  # 海表温度采集脚本与数据
│   ├── ci_download_toolkit/                   # 电网碳强度采集脚本与数据
│   └── offshore_wind_download_toolkit/        # ERA5 海上风电输入数据与下载清单
├── figures/                                   # 图表输出目录
└── results/                                   # 计算结果输出目录
```

## 数据说明

主要输入数据包括：

- `data/target_city_map.csv`：城市/都市圈、区域、经纬度、沿海分类、代表海点坐标。
- `data/Workload/*.csv`：小时级 CPU 负载曲线，要求包含 `cpu_load` 列，取值通常在 0 到 1 之间。
- `data/epw_download_toolkit/epw_files/*.epw`：220 个城市/都市圈匹配的 EPW/TMYx 气象文件。
- `data/sst_download_toolkit/sea_surface_temperature_2025_openmeteo.csv`：非 Inland 城市的小时级海表温度，单位为 degC。
- `data/ci_download_toolkit/carbon_intensity_electricitymaps.csv`：小时级电网碳强度宽表，单位按脚本约定为 gCO2eq/kWh。
- `data/offshore_wind_download_toolkit/*.nc`：严格沿海城市代表海点的 ERA5 风电气象输入。当前部分文件扩展名为 `.nc`，实际是 ZIP 容器，代码会自动读取内部 netCDF 文件。
- `data/offshore_wind_download_toolkit/strict_coastal_offshore_wind_points_manifest.csv`：城市与海上风电代表点、ERA5 网格点的对应关系。

下载和再生成数据的脚本位于各自的 `*_download_toolkit` 目录中，并附带局部 README 或 manifest。

## 环境依赖

核心能耗计算依赖：

```bash
pip install numpy pandas
```

海上风电 `.nc`/netCDF 数据读取和装机容量估算还需要：

```bash
pip install xarray netCDF4
```

如需重新下载 EPW 文件，还需要：

```bash
pip install requests
```

Electricity Maps 数据下载需要设置 API Token。

## 快速开始

列出可用城市：

```bash
python -m core.calculate_datacenter_energy --list-cities
```

计算单个城市的空气源/常规冷却结果：

```bash
python -m core.calculate_datacenter_energy ^
  --city "Shanghai" ^
  --cooling air_source ^
  --rated-it-power-kw 20000 ^
  --hours 8760 ^
  --time-alignment latest
```

计算单个城市的海水源冷却结果：

```bash
python -m core.calculate_datacenter_energy ^
  --city "Shanghai" ^
  --cooling seawater ^
  --rated-it-power-kw 20000 ^
  --hours 8760 ^
  --time-alignment sst ^
  --json
```

计算满足单城市年度数据中心总电耗所需的海上风电装机容量：

```bash
python -m core.calculate_wind_capacity ^
  --city "Shanghai" ^
  --cooling seawater ^
  --rated-it-power-kw 20000 ^
  --hours 8760 ^
  --json
```

运行严格沿海城市批量基准测试：

```bash
python run_baseline.py ^
  --rated-it-power-kw 20000 ^
  --idle-power-fraction 0.3 ^
  --hours 8760 ^
  --max-carbon-gap-hours 6 ^
  --output-dir results
```

## 输出结果

单城市数据中心计算会在 `results/` 下生成：

```text
datacenter_energy_<city>_<cooling_type>_<rated_power>.csv
```

单城市风电装机容量计算会生成：

```text
wind_capacity_<city>_<cooling_type>_<rated_power>.csv
```

批量基准测试会生成三张表：

```text
baseline_air_source_results_<rated_power>_<hours>.csv
baseline_seawater_results_<rated_power>_<hours>.csv
baseline_summary_<rated_power>_<hours>.csv
```

两张城市结果表包含：

- 服务器能耗、服务器碳排放。
- 冷却系统能耗、冷却系统碳排放。
- 总能耗、总碳排放。
- 所需海上风电装机容量。
- 对应装机容量下的风电全年发电量。
- 风电代表点、1 MW 年发电量、平均净容量因子和风电数据时间窗口。

汇总表包含三行：

- `air_source_all_regions`：空气源热泵所有纳入地区汇总。
- `seawater_all_regions`：海水源热泵所有纳入地区汇总。
- `seawater_savings_pct_vs_air_source`：海水源相对空气源的节约百分比。

## 模型说明

核心模型分为五层：

1. `core/datacenter.py` 建立机架、CPU/服务器、IT 风扇、CRAC、冷水机组、冷却塔和 HVAC 调用接口。
2. `core/seawater_heat_pump.py` 建立海水源热泵工程模型，覆盖冷冻水回路、海水取排水回路、板式换热器、热泵机组性能曲线和运行控制。
3. `core/calculate_datacenter_energy.py` 将城市气象、海表温度、碳强度和工作负载对齐为小时序列，并调用详细模型计算能耗和排放。
4. `core/calculate_wind_capacity.py` 读取城市对应 ERA5 海上风电输入，计算 1 MW 风电全年发电量，并按年度电量平衡反推所需装机容量。
5. `run_baseline.py` 遍历 `target_city_map.csv` 中 `Coastal class == Strict coastal` 的城市，分别计算空气源和海水源结果，再输出两类城市结果表和一张汇总表。

### 时间对齐

`--hours` 仍表示模拟小时数，但碳强度不再取 `carbon_intensity_electricitymaps.csv` 的前 `hours` 行：

- `seawater` 默认使用 `--time-alignment sst`，以 SST 文件的 timestamp 作为主时间轴，并把碳强度精确对齐到同一小时。
- `air_source` 默认使用 `--time-alignment latest`，使用碳强度文件中最新的 `hours` 小时时间窗口。
- `run_baseline.py` 默认使用 SST 时间窗口对比空气源和海水源，保证两种冷却方式使用同一时期碳排放因子；空气源温度仍按该 timestamp 映射到 EPW 典型气象年。
- 指定 `--start-time "2025-01-01 00:00"` 时会自动切换为 `start_time` 模式，从该时刻开始截取 `hours` 小时。
- 碳强度缺少少量小时会在对齐后按时间插值，默认最大连续缺口为 6 小时，可通过 `--max-carbon-gap-hours` 调整；超过阈值会报错。
- EPW 干球温度仍按 8760 小时典型气象年读取，并根据目标 timestamp 的 day-of-year/hour 映射到仿真时间轴。

### 海水源热泵模型

当前海水源热泵不使用单一简化 COP 模型，而是采用更细的工程子模型组合：

- 冷冻水回路：根据供回水温差、比热和泵效率计算冷冻水流量与泵功耗。
- 海水取排水回路：根据海水允许温升、管线长度、管径、粗糙度、局部损失、静扬程和泵效率计算海水流量、压降和泵功耗。
- 板式换热器：使用换热器 UA、有效度、NTU、夹点温差和可用换热量判断自然冷却能力。
- 热泵机组：优先使用配置中的性能曲线计算可用制冷量和 COP；缺少曲线时使用 Carnot 近似作为回退。
- 控制逻辑：按小时判断自然冷却、混合冷却或机械热泵模式，并跟踪未满足冷量和约束违规。

海水冷却的总冷却功率由冷冻水泵、海水泵、换热器辅助功耗、热泵压缩机功耗和其他辅助功耗组成。模型会同步输出有效 COP、压缩机 COP、自然冷却比例、机械冷却比例、排海温升、源侧流量和约束状态。

### 海上风电装机容量模型

`core.calculate_wind_capacity` 只做年度电量匹配：

```text
required_wind_capacity_mw = datacenter_total_energy_mwh / wind_generation_per_mw_mwh
```

其中 `wind_generation_per_mw_mwh` 来自 ERA5 小时级风速、温度和气压数据：

- 使用 10 m 和 100 m 风速估计 hub height 风速。
- 使用空气密度对功率曲线做一阶修正。
- 使用通用海上风电三段式功率曲线和损失系数估算净容量因子。
- 不做小时级供需平衡，不模拟储能、弃风、并网约束或 24/7 碳匹配。

## 数据再生成

### 海表温度

```bash
python data/sst_download_toolkit/collect_sst_openmeteo_quick.py ^
  --input data/target_city_map.csv ^
  --year 2025 ^
  --output data/sst_download_toolkit/sea_surface_temperature_2025_openmeteo.csv
```

### 电网碳强度

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

### EPW 气象文件

```bash
cd data/epw_download_toolkit
python download_epw_from_manifest.py --save-epw-dir --save-validated-manifest
```

### 海上风电 ERA5 输入

```bash
python data/offshore_wind_download_toolkit/download_era5_strict_coastal_wind_inputs.py
```

## 注意事项

- 批量 baseline 中，缺少有效碳强度、SST 或风电输入数据的城市会被跳过，并在命令行输出跳过原因。
- 仓库包含较大的 CSV、EPW 和 ERA5 数据文件，公开发布前应确认数据授权、引用方式和文件体积是否符合需求。
- `figures/` 和 `results/` 可作为运行输出目录，不应手动依赖其中的临时文件。
- 年度绿电覆盖不等同于小时级 24/7 零碳；小时级匹配、储能、蓄冷和负荷调度需要在后续供给侧模型中进一步扩展。
