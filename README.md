<p align="center">
  <img src="figures/logo.png" alt="Logo" width="200"/>
</p>

# Coastal Zero-Carbon Datacenter

沿海数据中心零碳潜力评估工具包，用于比较常规空气源冷却与海水源冷却在不同城市中的能耗、PUE 和碳排放表现，并为“沿海数据中心 + 海水冷却 + 海上风电/绿电”研究框架提供可复现的数据与模型基础。

项目围绕未来数据中心增长的沿海布局展开，结合全球城市/都市圈清单、EPW 气象数据、Open-Meteo 海表温度、电网碳强度和数据中心工作负载曲线，评估沿海城市中海水冷却相对常规冷却的节能减排潜力。

## 功能概览

- 单城市数据中心能耗和排放计算。
- 严格沿海城市批量基准测试。
- 空气源/常规冷却与海水源冷却对比。
- 基于 EPW 文件读取室外干球温度。
- 基于 Open-Meteo 海表温度数据驱动海水冷却模型。
- 基于 Electricity Maps 碳强度数据估算运行排放。
- 详细海水源热泵模型，覆盖自然冷却、混合冷却和机械热泵三种运行模式。
- 输出泵、压缩机、换热器辅助功耗、有效 COP、未满足冷量和约束违规等诊断指标。
- 提供 EPW、海表温度、碳强度数据下载与校验脚本。

## 目录结构

```text
.
├── run_baseline.py                          # 严格沿海城市批量对比入口
├── coastal_data_center_outline_v2.md         # 研究框架与论文提纲
├── core/
│   ├── calculate_datacenter_energy.py        # 单城市能耗/排放计算入口
│   ├── datacenter.py                         # 数据中心 IT 与 HVAC 详细模型
│   └── seawater_heat_pump.py                 # 海水源热泵、取排水、换热器和控制模型
├── utils/
│   ├── dc_config.json                        # 当前默认数据中心配置
│   ├── dc_config_reader.py                   # JSON 配置读取器
│   └── dc_config*.json                       # 其他数据中心配置样例
├── data/
│   ├── target_city_map.csv                   # 220 个城市/都市圈及沿海分类
│   ├── Workload/                             # CPU 工作负载曲线
│   ├── epw_download_toolkit/                 # EPW 下载、校验脚本与气象文件
│   ├── sst_download_toolkit/                 # 海表温度采集脚本与数据
│   └── ci_download_toolkit/                  # 电网碳强度采集脚本与数据
├── figures/                                  # 图表输出目录
└── results/                                  # 计算结果输出目录
```

## 数据说明

主要输入数据包括：

- `data/target_city_map.csv`：城市/都市圈、区域、经纬度、沿海分类、代表海点坐标。
- `data/Workload/*.csv`：小时级 CPU 负载曲线，要求包含 `cpu_load` 列，取值通常在 0 到 1 之间。
- `data/epw_download_toolkit/epw_files/*.epw`：220 个城市/都市圈匹配的 EPW/TMYx 气象文件。
- `data/sst_download_toolkit/sea_surface_temperature_2025_openmeteo.csv`：非 Inland 城市的小时级海表温度，单位为 degC。
- `data/ci_download_toolkit/carbon_intensity_electricitymaps.csv`：小时级电网碳强度宽表，单位按脚本约定为 gCO2eq/kWh。

下载和再生成数据的脚本位于各自的 `*_download_toolkit` 目录中，并附带局部 README。

## 环境依赖

核心计算依赖：

```bash
pip install numpy pandas
```

如需重新下载 EPW 文件，还需要：

```bash
pip install requests
```

碳强度下载脚本和海表温度采集脚本主要使用 Python 标准库；Electricity Maps 数据下载需要设置 API Token。

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
  --hours 8760
```

计算单个城市的海水源冷却结果：

```bash
python -m core.calculate_datacenter_energy ^
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
  --output-dir results
```

## 输出结果

单城市计算会在 `results/` 下生成形如以下格式的 CSV：

```text
datacenter_energy_<city>_<cooling_type>_<rated_power>.csv
```

批量基准测试会生成两个汇总文件：

```text
baseline_strict_coastal_all_results_<rated_power>_<hours>.csv
baseline_strict_coastal_global_savings_<rated_power>_<hours>.csv
```

结果字段包括：

- 基础能耗与排放：IT 能耗、冷却能耗、总能耗、碳排放、平均 IT 功率、平均冷却功率、平均 PUE。
- 温度统计：室外干球温度、海表温度、源温度均值和极值。
- 海水冷却运行状态：自然冷却小时数、混合冷却小时数、机械热泵小时数。
- 海水系统能耗分解：海水取排水泵能耗、冷冻水泵能耗、压缩机能耗、换热器辅助能耗。
- 模型诊断：平均有效 COP、平均压缩机 COP、未满足冷量、约束违规小时数。

## 模型说明

核心模型分为四层：

1. `core/datacenter.py` 建立机架、CPU/服务器、IT 风扇、CRAC、冷水机组、冷却塔和 HVAC 调用接口。
2. `core/seawater_heat_pump.py` 建立海水源热泵工程模型，覆盖冷冻水回路、海水取排水回路、板式换热器、热泵机组性能曲线和运行控制。
3. `core/calculate_datacenter_energy.py` 将城市气象、海表温度、碳强度和工作负载对齐为小时序列，并调用详细模型计算能耗和排放。
4. `run_baseline.py` 遍历 `target_city_map.csv` 中 `Coastal class == Strict coastal` 的城市，分别计算空气源和海水源冷却结果，再汇总节能减排效果。

### 海水源热泵模型

当前海水源热泵已不再使用单一简化 COP 模型，而是采用更细的工程子模型组合：

- 冷冻水回路：根据供回水温差、比热和泵效率计算冷冻水流量与泵功耗。
- 海水取排水回路：根据海水允许温升、管线长度、管径、粗糙度、局部损失、静扬程和泵效率计算海水流量、压降和泵功耗。
- 板式换热器：使用换热器 UA、有效度、NTU、夹点温差和可用换热量判断自然冷却能力。
- 热泵机组：优先使用配置中的性能曲线计算可用制冷量和 COP；缺少曲线时使用 Carnot 近似作为回退。
- 控制逻辑：按小时判断自然冷却、混合冷却或机械热泵模式，并跟踪未满足冷量和约束违规。

海水冷却的总冷却功率由冷冻水泵、海水泵、换热器辅助功耗、热泵压缩机功耗和其他辅助功耗组成。模型会同步输出有效 COP、压缩机 COP、自然冷却比例、机械冷却比例、排海温升、源侧流量和约束状态，便于校验不同城市与不同海水温度条件下的运行差异。

### 海水模型配置

海水系统参数通过 `utils/dc_config.json` 和 `utils/dc_config_reader.py` 读取。可配置内容包括：

- 冷冻水供回水温度、最小流量、冷冻水泵效率。
- 海水设计温升、最大排放温度、取排水管线长度、管径、粗糙度、局部损失系数、静扬程和海水泵效率。
- 板式换热器 UA、压降、接近温差、辅助功耗系数和是否启用自然冷却。
- 热泵额定制冷量、压缩机效率、最小 COP、最大 COP、部分负荷限制和性能曲线。
- 混合冷却、温升约束、机械冷却回退和诊断输出相关控制参数。

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

## 注意事项

- 仓库包含较大的 CSV 和 EPW 数据文件，公开发布前应确认数据授权、引用方式和文件体积是否符合需求。
- `utils/dc_config.py` 已标注为旧式配置文件；实际默认配置来自 `utils/dc_config.json`。
- `figures/` 和 `results/` 当前可作为运行输出目录，不应手动依赖其中的临时文件。
- 年度绿电覆盖不等同于小时级 24/7 零碳；小时级匹配、储能、蓄冷和负荷调度需要在后续供给侧模型中进一步扩展。

## 研究背景

本项目支撑的研究问题是：在全球数据中心持续增长的背景下，如果部分新增算力容量布局在沿海城市，并使用海水冷却降低冷却能耗、通过海上风电或其他零碳电力降低运行排放，该路径能否支撑年度绿电平衡甚至小时级零碳运行。

研究框架详见 `coastal_data_center_outline_v2.md`。
