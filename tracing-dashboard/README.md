# tracing-dashboard

轻量分布式链路追踪看板。服务把调用片段（span）批量上报到后端，后端负责：

1. **校验与去重**：非法片段（缺追踪/片段编号、时间缺失或倒置等）被拒绝并返回结构化错误；同一 `span_id` 重复上报只保留最早一份。
2. **乱序拼接调用树**：子片段先到会先挂起等待父片段；父片段一到立即归位；超过可配置的最长等待时间仍等不到（或父编号根本不存在）则归到「父片段缺失」占位节点下，不影响其它片段入树。
3. **关键路径与耗时占比**：从根到最耗时叶子的路径、各服务在本次请求中的耗时占比。
4. **依赖图聚合与环检测**：持续从拼好的调用树提取「谁调用谁」，按最近 1 小时 / 1 天窗口聚合调用次数与平均耗时，Tarjan 算法标记成环边；切换窗口重新聚合，新旧窗口不串数据。
5. **入口基线与时间维度对照**：把请求按**根片段所属服务 + 入口标识 `operation`** 归到同一入口，为每个入口增量维护两份“平时的样子”：端到端耗时分布（P² 算法增量更新 p50/p95/p99）与调用边出现频率（常规路径）。新请求拼好后**先用并入前基线判定、再并入样本**，产出对照结果——是否慢于平时（超过 p95 的可配置倍数）、多了哪些基线里没有的调用边、少了哪些常规边。样本不足可配置门槛时一律标“基线尚未成型”；无清晰根片段（全挂占位节点 / 多根）的请求不归类、不污染任何基线。
6. **实时推送**：新片段入库、依赖图更新、入口基线更新通过 SSE 推到前端。

前端是纯展示看板：请求检索列表 + 树状瀑布图（失败片段高亮、点击看详情）+ 服务依赖图（节点大小按频次、环边高亮、窗口切换）+ 入口基线对照（入口列表含样本量/延迟分位/最近异常，点开异常请求与所属基线并排对照，不做任何手工标注编辑）。

## 对照判定的关键约定

* **入口归类**：只认根片段（`parent_span_id = null`）。根片段的 `service` 与其 `operation`（缺省归一为 `default`）构成入口身份；没有唯一清晰根片段的请求（整棵树挂在「父片段缺失」占位节点下、或有多个根）不参与归类。
* **先判定，后并样本**：每个请求对照的基线版本 = 并入前该入口的样本条数。对照结果（分位快照、新增/消失边、所依据的版本号）落定后该请求才进入样本池——异常请求不可能在判定时被自己稀释。
* **定版时机**：一棵树所有片段都已归位（无挂起片段）时定版；定版后晚到的片段仍进树展示，但不再改基线。
* **基线只在内存增量维护，不另行落库**：重启时按片段开始时间（再按入库时间）重放，基线版本与对照结果确定性重建。

## 目录结构

```
backend/
  app/
    main.py            # ASGI 入口与生命周期
    config.py          # 环境变量配置（最长等待时间、DB 路径等）
    models.py          # 请求/响应 schema 与结构化错误
    validation.py      # 片段校验（脏数据拒绝）
    storage.py         # SQLite 持久化（片段；含 operation 列迁移、回放次序）
    tree.py            # 调用树拼接：挂起等待、占位节点、关键路径、耗时占比
    dependencies.py    # 依赖图聚合 + Tarjan 环检测
    entries.py         # 入口归类（根服务+operation）与请求样本归集
    latency_baseline.py  # 延迟基线：P² 增量维护 p50/p95/p99
    structure_baseline.py # 结构基线：调用边出现频率与常规边判定
    comparison.py      # 对照判定（纯函数）：延迟劣化 / 新增边 / 消失边
    baselines.py       # 入口基线注册表：先判后并、版本号、最近异常、重启回放
    events.py          # SSE 实时事件总线
    service.py         # 门面：串联存储 / 拼接 / 聚合 / 基线
    api/
      __init__.py
      http.py          # HTTP 路由：上报、检索、树、依赖图、入口与对照
      push.py          # SSE 推送路由
      deps.py          # FastAPI 依赖注入
  tests/               # 自动化测试（拼树/去重/缺父/顺序无关/环检测/脏数据/窗口切换/
                       #   延迟劣化/结构新增消失边/先判后并/样本不足/无根不入样/重启回放）
frontend/
  src/
    main.jsx           # React 入口
    App.jsx            # 页面外壳与 Tab 切换
    api.js             # HTTP + SSE 客户端
    components/
      TraceList.jsx    # 请求检索列表
      Waterfall.jsx    # 树状瀑布图
      SpanDetail.jsx   # 片段详情
      DependencyGraph.jsx # 服务依赖图（含环边高亮）
      EntryList.jsx    # 入口基线浏览（样本量/分位/最近异常）
      ComparisonDetail.jsx # 请求与入口基线的对照详情
docker-compose.yml     # 后端(python:3.12-slim) + 前端构建(node:20-alpine) + nginx(alpine)
```

## 一次构建启动

```bash
docker compose up --build
# 打开 http://localhost:8080
```

## 本地开发

```bash
cd backend
pip install -r requirements.txt -r requirements-dev.txt
pytest
cd ../frontend
npm install && npm run dev
```

## 上报示例

```bash
curl -X POST http://localhost:8080/api/spans \
  -H 'Content-Type: application/json' \
  -d '{"spans": [{
    "trace_id": "t1", "span_id": "s1", "parent_span_id": null,
    "service": "gateway", "operation": "place_order",
    "start_time": 1000, "end_time": 1300,
    "status_code": 200
  }]}'
```

响应中 `accepted` / `rejected` 分别列出接受和拒绝的片段；被拒绝项带机器可读的 `code` 与字段级说明。

根片段上的可选字段 `operation` 是这次请求打的对外操作标识，与根服务一起决定入口；非根片段上的该字段忽略，缺省归一为 `default`。

## 入口对照相关接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/entries` | 入口列表：样本量、是否成型、p50/p95/p99、常规边数、最近异常请求 |
| `GET /api/entries/detail?service=&operation=` | 单个入口的完整基线（延迟分位 + 每条边出现率/是否常规 + 最近异常） |
| `GET /api/comparisons/{trace_id}` | 某次请求的对照结果：入口、所依据基线版本、延迟档位与阈值、新增边、消失边 |

## 可配置项（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TRACING_BASELINE_MIN_SAMPLES` | 20 | 入口基线成型所需最低样本条数；不足时只给“基线尚未成型” |
| `TRACING_LATENCY_SLOW_MULTIPLIER` | 1.5 | 端到端耗时超过基线 p95 的该倍数判为延迟劣化 |
| `TRACING_STRUCTURE_REGULAR_RATIO` | 0.8 | 调用边出现率达到该比例才算入口常规路径 |

演示数据（同入口 18 次常规请求 + 一次延迟劣化 + 一次新增边 + 一次消失边 + 少量未成型样本）：

```bash
docker compose exec backend python scripts/seed_demo.py
```
