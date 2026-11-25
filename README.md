# 道路养护工时纠错服务（含跨月班次报送）

道路养护工时必须归到预算科目才能入仓。本服务用 FastAPI + SQLite 管理同月三张表，
判定每条工时的完整路径（工单 → 项目 → 预算科目），对重复工时提供事务化纠错；
并支持**月底跨夜班次报送**：按北京时间月界把一个班次拆到两个月份，参与同一套供数与纠错。

## 一、数据模型（同月每表行号唯一）

| 表 | 含义 | 关键字段 | 主键 |
| --- | --- | --- | --- |
| `work_hour` | 工时表 | 工单号 `work_order`、分钟数 `minutes`、来源 `source`(`manual`/`shift`)、`shift_id` | `(month, row_id)` |
| `dispatch` | 分派表 | 工单 → 项目 | `(month, row_id)` |
| `attribution` | 归科表 | 项目 → 预算科目 `budget_subject` | `(month, row_id)` |
| `correction_audit` | 纠错审计 | 撤除的表/行号、对应工时行 | 自增 id |
| `shift_revision` | 班次全部修订 | 班次号 + 修订号、工单、规范化北京起止时刻、原始回执 JSON | `(shift_id, revision)` |
| `shift_month_row` | 班次↔月份固定行号（永久保留） | `shift_id`、`month`、`row_id` | `(shift_id, month)`，另 `UNIQUE(month,row_id)` |

旧版本数据库（无 `source/shift_id` 列、无班次表）启动时自动幂等迁移：
旧工时全部标记为 `source='manual'`，命名卷升级不丢数据。

## 二、供数核算规则（集合式，不逐条展开路径）

完整路径 = 工时按工单号连分派、分派按项目连归科。核算由**一条集合式 GROUP BY SQL**
完成（见 `app/services.py` 的 `PATH_SQL`），服务端不按工时逐行 JOIN、不在 Python 追链：

- `path_count = 1`：**可供数**，返回工时及唯一预算科目；
- `path_count = 0`：扣留，原因 `no_path`；
- `path_count >= 2`：扣留，原因 `duplicate`。**两条路径即使落到同一预算科目也算重复**
  （计数按 分派×归科 组合，而非按科目去重）。

班次拆分出的工时就是 `work_hour` 中 `source='shift'` 的普通行，因此**原样参与供数与纠错**，
纠错时 `work_row_id` 直接用回执里的行号。

## 三、纠错事务语义

`POST /months/{month}/corrections` 指定工时行号 + 要撤除的一条分派或归科记录，
在单个 `BEGIN IMMEDIATE` 事务内完成：

1. 以**撤除前已提交数据**为快照校验：工时行、被撤记录必须存在；被撤记录必须在该工时
   的某条完整路径上；目标工时当前必须为重复（路径数 ≥ 2）；
2. 撤除记录并写审计；
3. 撤除后重新集合核算，强制两条不变量：
   - 目标工时恰好剩 1 条路径（能供数）；
   - 原本可供数的工时全部保有科目（共享归科被误撤会在此被拦）；
4. 校验通过才 COMMIT，撤除与供数变化一起生效，返回 `restored_work_row_ids`
   （本次由扣留恢复为可供数的全部工时行号）；
5. 任一校验失败即 ROLLBACK，数据保持撤除前状态，响应体用中文说明原因
   （404 不存在 / 422 撤错记录 / 409 违反供数或保科规则）。

并发：所有写事务均为 `BEGIN IMMEDIATE` + WAL + `busy_timeout`，SQLite 写锁使并发写入
在数据库层串行化，后到事务读到的必是前一个已提交版本；锁冲突返回 409 提示重试。

## 四、班次报送规则（本次新增）

### 4.1 入参与月界拆分

`POST /shifts`（在 `/docs` “班次报送”分组可直接试）JSON 字段：

| 字段 | 要求 |
| --- | --- |
| `shift_id` | 班次号，数字或字符串均可（兼容别名 `shift_no`/`shift_number`/`shift_code`） |
| `revision` | **正整数**修订号（兼容 `revision_no`/`rev`） |
| `work_order` | 工单号（兼容 `work_order_no`） |
| `start_at` / `end_at` | **北京时间**起止时刻，精确到分钟；naive 字符串按北京时间解释，带时区（含 `Z`）先换算到 UTC+8；兼容 `start_time`/`end_time` 等别名 |

约束：结束必须晚于开始；时长 `1…1440` 分钟，**最长 24 小时（含恰好 24h）**，违反返回 422。

拆分：从开始时刻起，每遇北京时间每月 1 日 00:00 切一段，`[start,end)` 按分钟求差，
**只给分钟数 > 0 的月份生成工时行**。24h 班次最多跨一个月界。例：
`2026-10-31 22:00 → 2026-11-01 06:00`（480 分钟）拆成
**2026-10 = 120 分钟、2026-11 = 360 分钟**。

### 4.2 回执

```json
{
  "shift_id": "YB-1001",
  "revision": 1,
  "work_order": "WO-7788",
  "start_at": "2026-10-31T22:00",
  "end_at": "2026-11-01T06:00",
  "total_minutes": 480,
  "months": [
    {"month": "2026-10", "row_id": 1, "work_row_id": 1, "minutes": 120},
    {"month": "2026-11", "row_id": 1, "work_row_id": 1, "minutes": 360}
  ]
}
```

`months` 即“各月的行号和分钟数”；同时回 `rows`/`month_rows`/`allocations`、
`by_month`、`shift_no`、`revision_no` 等别名键，便于不同调用方直接取值。
原样重放时响应体与首次回执**逐字段一致**，仅额外加响应头 `X-Idempotent-Replay: true`。

### 4.3 修订、幂等与并发

全部判定在 `BEGIN IMMEDIATE` 之后读取“写入前最新生效修订”完成，新修订在**同一事务**内
`DELETE` 该班次全部旧工时行 → 写入各月新行 → 落修订与回执：

- 库里没有该班次：任意正整数修订都接受；
- `revision < 最新生效修订`：**旧号，409 退回**；
- `revision == 最新`：工单号与规范化北京起止时刻完全一致 → **幂等返回存储的原回执**；
  内容不同 → **同号不同内容，409 退回**；
- `revision > 最新`：接受（允许跳号），一次替换该班次在各月的工时；
- 任何校验/写入失败都 ROLLBACK，**原修订、原工时完整保留**；
- 并发同号异内容竞争：写锁串行化后**恰好一个成功，另一个读到最新修订而 409**；
  并发原样重报：后来者按最新版重放，全部 200 且回执一致。

### 4.4 固定行号与手工工时边界

- 班次首次进入某月时分配行号 = 该月现存工时行与全部班次保留号的 `MAX(row_id)+1`
  （空月第一班即 1），并在 `shift_month_row` 记 `(shift_id, month) -> row_id`；
- **同一班次在同一月行号永久固定**：新修订改变时段、班次移出该月（删除该月工时行），
  映射都保留；以后修订补回该月，**仍用最初行号**；
- `POST /months/{month}/work-hours` 手工录入时，只要任一行号命中该月
  `shift_month_row` 保留号，**整批 409 退回**，不先删后写、同批其他行也不落库；
- 工时表 `replace=true` 整月覆盖改为**只删 `source='manual'` 的手工行**，
  班次工时原样保留；
- `DELETE /months/{month}` 在该月**当前存在 `source='shift'` 工时行时返回 409 拒绝清空**；
  班次已移出、当月无班次工时时允许清空（保留号映射不动，补回仍用原号）。

## 五、启动（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000/docs
# “班次报送”分组：POST /shifts、GET /shifts、GET /shifts/{shift_id}
```

SQLite 文件落在命名卷 `maintenance-data:/data/maintenance.db`，容器重建数据不丢，
并自动完成旧库加列迁移。

等价的本机裸跑（与镜像一致的 Python + `requirements.txt`）：

```bash
pip install -r requirements.txt
APP_DB_PATH=/tmp/maintenance.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 六、接口一览（均在 /docs 可试）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/shifts` | **班次报送/修订**（另有 `/shift-reports`、`/api/shifts`、`PUT /shifts/{id}`、`POST /months/{month}/shifts` 等兼容别名） |
| GET | `/shifts` | 全部班次的最新修订与各月行号 |
| GET | `/shifts/{shift_id}` | 该班次最新回执 + 全部修订历史 |
| POST | `/months/{month}/work-hours` | 手工录入工时（`replace=true` 只覆盖手工行；占用班次保留号整批 409） |
| POST | `/months/{month}/dispatches` | 录入分派表 |
| POST | `/months/{month}/attributions` | 录入归科表 |
| GET | `/months/{month}/supply` | 供数视图：可供数带科目，扣留带路径数与原因 |
| POST | `/months/{month}/corrections` | 纠错：撤除一条分派/归科（班次行用回执行号） |
| GET | `/months/{month}/audit` | 纠错撤除审计 |
| DELETE | `/months/{month}` | 测试辅助：清空某月（有班次工时 409） |
| GET | `/docs` / `/openapi.json` | Swagger 演示与契约 |
| GET | `/health` | 健康检查（容器 HEALTHCHECK） |

`month` 格式固定 `YYYY-MM`。跨月班次示例：

```bash
# 首报：10/31 22:00 -> 11/01 06:00（北京），拆成 10 月 120 分、11 月 360 分
curl -X POST localhost:8000/shifts -H 'Content-Type: application/json' \
  -d '{"shift_id":"YB-1001","revision":1,"work_order":"WO-7788",
       "start_at":"2026-10-31T22:00","end_at":"2026-11-01T06:00"}'

# 手工整月覆盖：只替换手工行，班次行保留
curl -X POST localhost:8000/months/2026-10/work-hours -H 'Content-Type: application/json' \
  -d '{"replace":true,"rows":[{"row_id":7,"work_order":"WO-HAND","minutes":45}]}'

# 用回执行号给班次工时做纠错（先录分派/归科造出重复路径）
curl -X POST localhost:8000/months/2026-10/corrections -H 'Content-Type: application/json' \
  -d '{"work_row_id":1,"remove":"dispatch","remove_row_id":2}'
```

## 七、实测结果

本机没有 Docker 守护进程/CLI，未能真正 `docker compose build/up`；已做
`docker-compose.yml` YAML 解析校验，并用与镜像同款依赖（Python 3.11 +
fastapi 0.115.6 / pydantic 2.10.4 / uvicorn 0.34）启动**真实 uvicorn 子进程 + HTTP** 试跑：

- **新增 `tests/run_shift_e2e.py`：78/78 项断言通过、进程退出码 0**（独立临时 DB、
  真实 HTTP、线程屏障并发），覆盖：
  - 跨月拆分 120/360、30/30 时区换算、月末 1 分钟边界、恰好 24h 接受、超 24h/
    结束早于开始/坏时刻/修订号 0 均 422；
  - 回执行号进入 `/supply`，造双路径后用该行号 `/corrections` 纠错并恢复供数；
  - 原样重报响应体与原回执完全一致且带重放头；同号不同内容 409、旧号 409，
    失败后分钟数与修订查询均保持原状；跳号修订接受；
  - **跨月修订 × 手工覆盖完整交错**：保留号占用整批退回且同批行不落库；
    `replace=true` 只换手工、旧手工被清、班次行不动；含保留号的覆盖批被拒后
    原手工行仍在（未先删后败）；修订移出月后行删除但号仍保留、手工仍被拒；
    有班次工时月份 DELETE 409 且数据不动，移出后可清空；再修订补回仍用原行号；
  - **并发**：6 个相同首报全部 200 且回执完全一致；同号异内容两并发恰好 1 成 1 败
    （409 中文原因），最终工单号/分钟与获胜事务一致；3 个相同新修订并发全部 200 同回执；
  - 别名路径/字段、`GET /shifts[/{id}]`、未知班次 404、`/docs`、`/openapi.json`、
    `/health` 均 200。
- **原有 `tests/run_e2e.py` 无回归：39 项 PASS、“全部断言通过”、退出码 0**
  （共享归科保科、同科目双路径、并发纠错等原行为不变）。
- 另做了两类手工/脚本核验：
  - 多班次同月分配为 1、2；已有手工行 40 后新班次取 41；班次进入另一月取 42；
  - **旧版库迁移**：按 1.0 结构造库并写旧工时，新版 `init_db()` 自动加列，
    旧行变 `source='manual'`，在其上跑班次（行号正确避开旧行 3 取 4）与手工覆盖均正确。
- 全部 Python 源 `py_compile` 通过；`/docs` 实测 HTTP 200。

复跑方式：

```bash
PYTHONPATH=/tmp/pylibs:. python3 tests/run_shift_e2e.py     # 端口可用 PORT 覆盖
APP_DB_PATH=/tmp/baseline.db PYTHONPATH=/tmp/pylibs python3 tests/run_e2e.py
```

## 八、未完成项 / 余项

1. **当前环境无 Docker**（`docker`/`podman`/`nerdctl` 均不存在），镜像未实际
   `docker compose build/up`，仅完成 compose YAML 解析、Dockerfile 核对与等价的
   本机 uvicorn 全量试跑；需在有 Docker 守护进程的机器上做一次真实启动确认。
2. 入参/回执额外提供了若干命名别名与冗余键（如 `shift_no`、`rows`、`data`），
   属于兼容性设计；若调用方契约固定，可在 `app/schemas.py` 与
   `services._build_shift_receipt` 中收敛。
3. 认证鉴权、分页未纳入范围；服务默认监听 8000，建议部署在内网或网关之后。
4. 测试仍为标准库脚本（新增 `tests/run_shift_e2e.py`，与原 `run_e2e.py` 同风格）；
   如引入 pytest 可直接搬迁断言。`requirements.txt` 附带了 `httpx`，便于 TestClient。
