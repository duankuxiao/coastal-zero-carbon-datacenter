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
- 提供 EPW、海表温度、碳强度数据下载与校验脚本。

## 目录结构

```text
.
├── run_baseline.py                         # 严格沿海城市批量对比入口
├── coastal_data_center_outline_v2.md        # 研究框架与论文提纲
├── core/
│   ├── calculate_datacenter_energy.py       # 单城市能耗/排放计算入口
│   └── datacenter.py                        # 数据中心 IT 与 HVAC 详细模型
├── utils/
│   ├── dc_config.json                       # 当前默认数据中心配置
│   ├── dc_config_reader.py                  # JSON 配置读取器
│   └── dc_config*.json                      # 其他数据中心配置样例
├── data/
│   ├── target_city_map.csv                  # 220 个城市/都市圈及沿海分类
│   ├── Workload/                            # CPU 工作负载曲线
│   ├── epw_download_toolkit/                # EPW 下载、校验脚本与气象文件
│   ├── sst_download_toolkit/                # 海表温度采集脚本与数据
│   └── ci_download_toolkit/                 # 电网碳强度采集脚本与数据
├── figures/                                 # 图表输出目录
└── results/                                 # 计算结果输出目录
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
python core/calculate_datacenter_energy.py --list-cities
```

计算单个城市的空气源/常规冷却结果：

```bash
python core/calculate_datacenter_energy.py ^
  --city "Shanghai" ^
  --cooling air_source ^
  --rated-it-power-kw 20000 ^
  --hours 8760
```

计算单个城市的海水源冷却结果：

```bash
python core/calculate_datacenter_energy.py ^
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

结果字段包括 IT 能耗、冷却能耗、总能耗、碳排放、平均 IT 功率、平均冷却功率、平均 PUE、平均 COP 和源温度统计等。

## 模型说明

核心模型分为三层：

1. `core/datacenter.py` 建立机架、CPU/服务器、IT 风扇、CRAC、冷水机组、冷却塔、海水换热/热泵等数据中心物理模型。
2. `core/calculate_datacenter_energy.py` 将城市气象、海表温度、碳强度和工作负载对齐为小时序列，并调用详细模型计算能耗和排放。
3. `run_baseline.py` 遍历 `target_city_map.csv` 中 `Coastal class == Strict coastal` 的城市，分别计算空气源和海水源冷却结果，再汇总节能减排效果。

海水冷却模型会根据海水温度判断是否可进行自然冷却；若不满足自然冷却条件，则按参数化热泵 COP、取排水泵功耗和辅助换热功耗计算总冷却功率。

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

- 当前目录最初不是 Git 仓库，推送前需要初始化 Git 并创建远程仓库。
- 仓库包含较大的 CSV 和 EPW 数据文件，公开发布前应确认数据授权、引用方式和文件体积是否符合需求。
- `utils/dc_config.py` 已标注为旧式配置文件；实际默认配置来自 `utils/dc_config.json`。
- `figures/` 和 `results/` 当前可作为运行输出目录，不应手动依赖其中的临时文件。
- 年度绿电覆盖不等同于小时级 24/7 零碳；小时级匹配、储能、蓄冷和负荷调度需要在后续供给侧模型中进一步扩展。

## 研究背景

本项目支撑的研究问题是：在全球数据中心持续增长的背景下，如果部分新增算力容量布局在沿海城市，并使用海水冷却降低冷却能耗、通过海上风电或其他零碳电力降低运行排放，该路径能否支撑年度绿电平衡甚至小时级零碳运行。

研究框架详见 `coastal_data_center_outline_v2.md`。
