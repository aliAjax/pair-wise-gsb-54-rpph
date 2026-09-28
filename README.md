# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口和接续质量和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
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
- `GET /api/records/{id}`：记录详情；进入分批复通后附带`restore_ledger`台账。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 分批复通台账

系统测试通过（`tested`）后，工单进入分批复通阶段（`restoring`），不再一次性恢复：

- `register_batch`：登记一批复通，`data`为`{"route":"R1","capacity_gbps":250,"customer_groups":["G1","G2"]}`。
  - 累计登记容量（含待确认批次，不含已撤销批次）不得超过故障前容量`capacity_gbps`。
  - 同一路由、同一客户组不能被两个未撤销批次重复占用。
  - 登记后批次为`pending`（待确认），容量尚不计入可用容量。
- `confirm_batch`：`{"seq":1}`，确认后批次转为`confirmed`，容量才计入可用容量。
- `revert_batch`：`{"seq":1}`，撤销只回退这一批：释放该批容量、路由与客户组占用，其他批次不受影响。
- `restore`：仅当已确认容量补齐故障前容量、且没有待确认批次时，工单才结束为`restored`，否则返回409。

记录详情中的`restore_ledger`包含：故障前容量、累计登记容量、已确认容量、剩余缺口、待确认批次数，以及每批的路由、容量、客户组、状态和确认/撤销操作人与时间。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及分批复通的容量累计、重复占用拦截、确认/撤销回退和结束门槛。
