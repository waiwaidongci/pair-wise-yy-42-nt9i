# 山火事件指挥与离线人员调度

维护火线、风向、资源和任务区，合并离线现场记录并防止人员重复分配。火场撤离调度已接通：巡火队和无人机回传火线折点、风级跃变和队员定位，值班台直接电子派工。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验（风级、折点、名单）。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、撤离计划重算和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制、占用唯一约束和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败和撤离调度测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8319
```

默认端口为`8319`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`、`POST /api/items`、`GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

### 火场撤离调度

- `POST /api/items/{id}/zones` 建任务区（incident_commander）；`GET /api/items/{id}/zones`、`GET /api/zones/{id}`（含关闭列清）。
- `POST /api/zones/{id}/reports` 现场回传，`kind`为`wind`/`breakpoints`/`location`，必带`report_no`；断网补报按单号合并，重试返回`merged:true`且只算一次。
- `POST /api/zones/{id}/dispatches` 派工，记录`order_no`（单号）、`breakpoints`（折点）、`wind_level`（缺省取任务区当前风级）、`members`（队员）；单号重复时合并返回原单。
- `GET /api/zones/{id}/dispatches`、`GET /api/dispatches/{id}`（含队员占用与计划历史）。
- `POST /api/dispatches/{id}/transition` 派工状态机：`planned→in_progress→completed→closed`（closed为签认，仅incident_commander），必须提交`expected_version`。
- `POST /api/dispatches/{id}/members/{mid}/resolve` 协调占用，`action`为`release`/`promote`（incident_commander）。
- `POST /api/zones/{id}/materials` 登记物资需求（logistics，按名称upsert）；`POST /api/zones/{id}/materials/{mid}/deliver` 到货回传，按`report_no`幂等。
- `GET /api/zones/{id}/closure_checklist` 关闭前列清：未归队队员、未齐物资、待协调占用。
- `POST /api/zones/{id}/close` 关闭任务区（incident_commander），列清未空则409并逐项列出。

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。

## 调度规则

- **先到占用**：同一事件内同一队员只保留一个`occupied`占用（部分唯一索引兜底），两个入口同时提交时先到者占用、后到者转`pending_coordination`，由指挥员`release`或`promote`协调。
- **断网补报**：派工按`order_no`、回传按`report_no`、到货按`report_no`合并，重试一次只算一次。
- **风级跃变**：风级回传与任务区当前风级不同时，未开工（planned）派工按新条件重算撤离窗口与风险并留存计划历史；已签认（closed）派工退回completed待重新确认；已完成、进行中行动保留原过程不变。
- **归队**：`location`回传`returned:true`将区内该队员占用置为`returned`。
- **关闭不变量**：任务区关闭前必须列清未归队队员、未齐物资和待协调占用，全部清空方可关闭。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
