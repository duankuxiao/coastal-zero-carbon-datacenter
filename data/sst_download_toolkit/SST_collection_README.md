# 海洋温度数据采集说明

## 输入
- `/mnt/data/代表国家及城市.xlsx`
- 目标 sheet：`Country_city_map`
- 筛选条件：`Coastal class != Inland`
- 经度/纬度：`Representative sea-point latitude`、`Representative sea-point longitude`

## 输出文件
1. `coastal_city_sea_points.csv`
   - 已从工作簿筛出的 124 个非 Inland 城市及代表海点经纬度。
2. `sea_surface_temperature_2023_TEMPLATE.csv`
   - 8760 行、非闰年 2023 小时索引、124 个城市列的空模板。
3. `collect_sst_openmeteo.py`
   - 可复现采集脚本。
4. `sst_fetch_attempt.log`
   - 本环境中实际联网尝试日志。本次失败原因为 DNS 无法解析 `marine-api.open-meteo.com`。

## 采集命令
在可联网环境运行：

```bash
python collect_sst_openmeteo_quick.py \
  --input target_country_city_map.xlsx \
  --year 2025 \
  --output sea_surface_temperature_2025_openmeteo.csv
```

输出 CSV 格式：

```text
timestamp,Silicon Valley / Santa Clara cluster,Los Angeles,...
2023-01-01 00:00,<degC>,<degC>,...
...
```

脚本会校验：
- 年份必须为非闰年；
- 每个城市必须返回 8760 个小时值；
- 时间戳必须与 `2023-01-01 00:00` 至 `2023-12-31 23:00` 完整对齐。

## 数据源
脚本调用 Open-Meteo Marine API，变量为 `sea_surface_temperature`，单位为摄氏度（degC）。
