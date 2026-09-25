# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突、航道通航冲突和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。动作包括`confirm`、`berth`、`depart`、`reschedule`、`cancel`。

## 航道通航调度

每条靠泊计划在创建时必须登记航道通航时段（整数小时，0-24模型）：

- `transit_start_hour`：通航开始小时（0-23）。
- `transit_end_hour`：通航结束小时（1-24），半开区间`[start, end)`，必须晚于开始。
- `draft_m`：船舶吃水，同时用于泊位水深和潮汐安全判断。

调度规则：

- **潮汐安全**：落潮/平潮期航道安全吃水默认9.0米（`DomainRules(tide_safe_draft_m=...)`可调）。吃水超过该值的深吃水船只能把通航时段整体排在涨潮窗内，默认涨潮窗为`[2,8)`和`[14,20)`（`flood_tide_windows`可配）。
- **独占时段**：危险品船（`dangerous_goods=true`）或超长船（船长≥250米，`exclusive_length_m`可配）独占通航时段，与任何其他船舶的时段重叠都冲突；普通船舶之间允许同窗口并排通过。
- **通航先于靠泊**：通航结束时间必须赶在靠泊开始（`eta_hour`）之前。创建时先登记；`confirm`时若已晚于ETA，记录整体转为`reschedule_required`（通航与靠泊一起待重排），`nav_status`同步标记并写明`reschedule_reason`。
- **重排恢复**：待重排记录执行`reschedule`动作，提交新的`transit_start_hour/transit_end_hour`，重新校验潮汐、独占和准时规则后恢复为`confirmed`；校验不通过则拒绝。
- **离泊释放**：`depart`离泊出港后`nav_status=released`，占用的通航时段自动让出；`cancelled`和`reschedule_required`状态同样不占航道，后续船舶可登记同时段。

payload中的派生字段：`deep_draft`（是否深吃水）、`flood_tide_required`、`channel_exclusive`（是否独占船）、`nav_on_time`、`nav_status`（registered/confirmed/reschedule_required/released）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、航道潮汐与独占冲突、迟到待重排与重排恢复、离泊释放、重复引用、权限拒绝和版本冲突。
