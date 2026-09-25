# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、航道通航调度（潮汐窗口、独占时段、通航时序）和冲突检查。
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
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 航道通航调度

每条靠泊计划创建时必须登记通航时段（`transit_start_hour`/`transit_end_hour`，0–24小时制），吃水沿用`draft_m`。规则如下：

- **潮汐约束**：吃水超过潮汐安全值（`TIDE_SAFE_DRAFT_M=10.0`米）的船舶，通航时段必须全部落在涨潮窗口（默认小时2–6、14–18，见`rules.py`），否则创建或重排直接拒绝。
- **独占时段**：危险品船或超长船（船长超过200米）独占通航时段，其窗口与任何其他船舶重叠即冲突（409）；普通船之间允许在同一时段并排通航。
- **通航先于靠泊**：通航结束必须不晚于靠泊开始（`transit_end_hour <= eta_hour`）。晚于靠泊不报错，`transit_status`与`berth_status`同时标记为`pending_reschedule`，并阻止`confirm`/`berth`；对记录执行`reschedule`动作（请求体提供新的`transit_start_hour`/`transit_end_hour`）重新校验潮汐与独占冲突，通过后恢复`registered`/`scheduled`。
- **自动让出**：执行`depart`离泊出港后，`transit_status`置为`released`，该时段不再参与冲突检查，其他船舶可立即使用；`cancel`同样释放。
- 待重排计划不占用航道时段，可在不解除原计划的情况下改排其他船舶。

创建记录的请求体示例：

```json
{"reference":"VOY-1","data":{"vessel":"ShenHai","berth":"B1","vessel_length_m":180,"berth_length_m":220,"draft_m":10.2,"berth_depth_m":11.5,"eta_hour":6,"etd_hour":18,"risk_level":"high","dangerous_goods":false,"dangerous_class":"","transit_start_hour":2,"transit_end_hour":6}}
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及航道潮汐限制、危险品/超长独占、通航时序待重排、重放冲突检查和离泊自动让出。
