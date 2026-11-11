# 水样检测结果快照取数 API

实验室数据员分页提取大批水样结果编制月报时，**先开快照、再翻页**：快照在创建时
固定当时已提交的修订号，之后任意时刻用游标翻页，看到的都是该修订下的记录与值——
翻页途中的更正、删除、新增都不会漏行、重行或混入新值。

技术栈：FastAPI + SQLite（WAL），Docker Compose 交付两个共享同一数据库文件的服务进程。

## 规则实现一览

| 需求 | 实现 |
| --- | --- |
| 已确认结果存 SQLite：唯一 ID、样品码、项目、检测日期、值（数字或字符串） | `result_versions` 表，值以 JSON 文本保存，类型往返无损 |
| 每批最多 100 条写入或按 ID 删除，同 ID 写入替换 | `POST /api/v1/batches`，1～100 条，同 ID 关闭旧版本写入新版本 |
| 整批成功才产生一个递增修订号，失败全不生效 | 批次内全部操作与修订行同事务提交；校验失败/异常整体回滚 |
| 首次取数：项目 + 日期闭区间 + 页大小 1～100，固定当时已提交修订 | `POST /api/v1/snapshots`，登记快照行并返回第一页 |
| 按检测日期、ID 升序返回首批及后续游标 | 键集游标 `(test_date, id)`，`ORDER BY test_date, id` |
| 翻页保留该修订下的记录和值（含后来被更正/移出/删除的），排除之后新增 | 版本链 `[valid_from, valid_to)` 按快照修订号判定可见性 |
| 同一游标重试返回相同页 | 游标为无状态令牌，历史版本不可变，查询结果确定 |
| 修订历史和游标重启后可用 | 全部状态在 SQLite 文件中；游标无状态，快照行持久化 |
| 两个服务进程并发写入/开快照/翻页 | WAL + `busy_timeout` + `BEGIN IMMEDIATE`，写者串行、读者不阻塞 |
| 按页载入，不全量载入、不复制整份结果表 | 每页一条带 `LIMIT` 的键集查询；快照只存筛选条件与修订号 |

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

启动两个服务进程，共享命名卷 `labdata` 中的同一个 SQLite 数据库：

- app1: http://localhost:8001 （交互文档 http://localhost:8001/docs）
- app2: http://localhost:8002 （交互文档 http://localhost:8002/docs）

运行跨页更正演示（写入走 app1、取数走 app2，验证两进程语义）：

```bash
python3 demo/cross_page_correction.py --base http://localhost:8001 --read-base http://localhost:8002
```

在容器内跑测试：

```bash
docker compose --profile test run --rm tests
```

## 本地开发

```bash
pip install -r requirements-dev.txt
pytest -q                                              # 34 个测试
uvicorn app.main:app --port 8001                       # 数据库默认 ./results.db
RESULTS_DB=/path/to/results.db uvicorn app.main:app    # 指定数据库文件
python3 demo/cross_page_correction.py --base http://localhost:8001
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/batches` | 提交一批写入/删除（1～100 条，原子生效，返回新修订号） |
| GET | `/api/v1/revisions` | 修订历史（重启后保留） |
| GET | `/api/v1/results/{id}` | 某 ID 在最新修订下的状态 |
| POST | `/api/v1/snapshots` | 开启快照：固定当前修订，返回第一页与游标 |
| GET | `/api/v1/snapshots/{id}` | 快照元数据 |
| GET | `/api/v1/snapshots/{id}/pages?cursor=...` | 按游标翻页（同一游标重试返回相同页） |
| GET | `/health` | 健康检查 |

### 批次写入

```bash
curl -X POST http://localhost:8001/api/v1/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "operations": [
      {"op": "put", "id": "R-001", "sample_code": "WS-001",
       "project": "COD", "test_date": "2026-10-02", "value": 12.5},
      {"op": "put", "id": "R-002", "sample_code": "WS-002",
       "project": "COD", "test_date": "2026-10-03", "value": "未检出"},
      {"op": "delete", "id": "R-000"}
    ]
  }'
# → {"revision": 1, "applied": 3, "puts": 2, "deletes": 1}
```

任何一条不合法（日期格式/不存在日期、缺字段、布尔或 null 值、批次内 ID 重复、
超过 100 条等）→ `422`，整批不生效、不产生修订号。

### 开启快照并翻页

```bash
# 第一页（固定当前修订）
curl -X POST http://localhost:8001/api/v1/snapshots \
  -H 'Content-Type: application/json' \
  -d '{"project": "COD", "date_from": "2026-10-01",
       "date_to": "2026-10-31", "page_size": 5}'
# → {"snapshot_id": "...", "revision": 1, ...,
#    "page": {"number": 1, "items": [...], "next_cursor": "v1...."}}

# 后续页（next_cursor 为 null 表示最后一页）
curl "http://localhost:8001/api/v1/snapshots/<snapshot_id>/pages?cursor=<next_cursor>"
```

## 跨页更正示例

月报取数途中发生更正时，已固定的快照页不受影响的完整时序
（`demo/cross_page_correction.py` 可实际运行并自带断言）：

| 步骤 | 操作 | 结果 |
| --- | --- | --- |
| 1 | 批次 1 写入 12 条 10 月 COD 结果 | 修订 1 |
| 2 | 开快照（COD，10-01～10-31，页大小 5），读第 1 页 | 快照固定修订 1 |
| 3 | 批次 2：改第 1 页某条的值、删第 2 页某条、把第 3 页某条日期改到 11 月、新增 1 条 | 修订 2 |
| 4 | 用旧游标继续读第 2、3 页 | 仍是原来 12 条与原值；被改/被删/被移出的记录原样保留，新增记录不出现 |
| 5 | 用同一游标重试第 2 页 | 响应与首次完全一致 |
| 6 | 重新开快照 | 看到修订 2 的状态：11 条，含更正后的值 |

## 设计说明

**版本链（`result_versions`）**：结果表只追加、从不原地修改。每行记录
`[valid_from, valid_to)` 修订区间，`valid_to IS NULL` 表示当前生效；删除写入
`deleted=1` 的墓碑版本。任意历史修订 R 下的可见状态 =
`valid_from <= R AND (valid_to IS NULL OR valid_to > R)`，因此快照无需复制数据。

**键集分页**：每页查询以上一页最后一条的 `(test_date, id)` 为起点
`LIMIT page_size + 1`（多取一条判断是否还有下一页）。应用按页载入记录，
从不整表载入，也不为取数复制整份结果表；`(project, test_date, id)` 索引
支撑筛选与排序。

**游标**：`v1.<base64url(json)>`，含快照 ID、页末 `(test_date, id)` 与页码。
无状态、可重试、重启后可用；与快照 ID 不匹配或格式非法返回 400。

**并发**：SQLite WAL 模式下读者不阻塞写者；所有写事务（批次、开快照）以
`BEGIN IMMEDIATE` 开始并带 30s `busy_timeout`，两个服务进程共享同一数据库
文件时写者串行提交，修订号由 `revisions` 自增主键分配，不会重复。
`tests/test_concurrency.py` 覆盖多线程与多进程并发场景。

## 项目结构

```
app/
  main.py      # FastAPI 应用工厂、路由、游标编解码
  db.py        # SQLite 存储层：修订、版本链、快照、键集翻页
  schemas.py   # Pydantic 请求/响应模型与校验
demo/
  cross_page_correction.py   # 跨页更正演示（仅标准库，自带断言）
tests/                       # 批次/快照/持久化/并发测试（pytest）
Dockerfile                   # 多阶段：runtime / test
docker-compose.yml           # app1 + app2 共享 SQLite 卷；test profile
```
