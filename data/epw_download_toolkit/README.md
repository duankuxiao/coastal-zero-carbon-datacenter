# EPW 下载与校验工具包

本工具包基于 `代表国家及城市.xlsx` 的 `Country_city_map`（sheet2）经纬度，已完成 220 个城市/都市圈到 Climate.OneBuilding TMYx 气象站的最近邻匹配。

## 文件说明

- `manifest_to_download.csv`：城市—气象站匹配清单，包含城市经纬度、匹配气象站、距离、Climate.OneBuilding ZIP URL、目标 EPW 文件名。
- `download_epw_from_manifest.py`：下载每个匹配站点 ZIP，提取 EPW，按城市行生成 EPW 文件，并校验 8760 小时、非闰年和关键列。
- `README.md`：本说明。

## 使用方式

在本目录运行：

```bash
python download_epw_from_manifest.py
```

如缺少 `requests`：

```bash
pip install requests
python download_epw_from_manifest.py
```

## 运行后输出

- `epw_files/`：220 个 EPW 文件，命名格式为 `行号_国家_城市_匹配站点_WMO.epw`。
- `validated_manifest.csv`：下载与校验结果。
- `epw_files.zip`：包含全部 EPW 和校验清单的压缩包。

## 校验规则

脚本会检查：

1. EPW 数据行是否为 8760 行；
2. 是否存在 2 月 29 日；
3. 第 7 列（索引 6）室外干球温度是否可解析为数值，单位 degC；
4. 第 9 列（索引 8）相对湿度是否可解析为数值，单位 %；
5. 第 10 列（索引 9）气压是否可解析为数值，单位 Pa。

## 匹配原则

- 先按国家 ISO3 代码筛选候选气象站；中国同时允许 `CHN` 和 `HKG`，香港行优先匹配 `HKG`。
- 用 Haversine 球面距离选择最近气象站。
- 同一气象站存在多个 TMYx 版本时，优先使用较新的 `2011-2025`，其次 `2009-2023`、`2007-2021`、完整 TMYx、`2004-2018`。

## 当前环境限制说明

本会话环境可以读取并匹配 Climate.OneBuilding 索引表，但对 ZIP 气象文件的直接下载被沙盒下载策略拦截。因此此包提供可复现的下载脚本；在普通联网 Python 环境中运行即可获取并校验全部 EPW 文件。
