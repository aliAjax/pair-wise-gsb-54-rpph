# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口、接续质量、分批复通台账和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询（含复通批次台账表）。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

### 分批复通台账

系统测试通过（`tested`）后不再一次性恢复，而是进入分批复通：先登记批次（工单进入`restoring`），逐批确认后容量计入可用，全部补齐方可结束（`restored`）。动作如下，均由`noc_operator`或`repair_manager`执行：

- `register_batch`：登记一批，数据为`{"route":"R-北","capacity_gbps":200,"customer_groups":["政企","金融"]}`，批次初始为待确认（`pending`）。所有未撤销批次（含待确认）的累计容量不得超过故障前容量`capacity_gbps`，同一路由或同一客户组不能在两个活动批次中重复占用，撤销后释放。
- `confirm_batch`：数据为`{"batch_no":1}`，确认后该批容量计入可用容量。
- `revoke_batch`：数据为`{"batch_no":1}`，只撤销并回退这一批（待确认或已确认均可），释放其容量、路由和客户组占用，其他批次不受影响。
- `complete_restoration`：无数据。仅当已确认容量补齐故障前容量且不存在待确认批次时，工单才转为`restored`。

`GET /api/records/{id}` 详情中的 `restoration` 字段给出台账：每批的路由、容量、客户组与状态，以及`confirmed_capacity_gbps`（可用容量）、`pending_capacity_gbps`、`remaining_gap_gbps`（剩余缺口）、`pending_batches`和`can_complete`。各批登记、确认、撤销和完工均写入审计时间线。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、分批复通台账（登记/确认/撤销/完工及超额、重复占用拦截）、重复引用、权限拒绝和版本冲突。
