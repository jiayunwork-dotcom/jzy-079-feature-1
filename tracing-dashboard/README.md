# tracing-dashboard

轻量分布式链路追踪看板。服务把调用片段（span）批量上报到后端，后端负责：

1. **校验与去重**：非法片段（缺追踪/片段编号、时间缺失或倒置等）被拒绝并返回结构化错误；同一 `span_id` 重复上报只保留最早一份。
2. **乱序拼接调用树**：子片段先到会先挂起等待父片段；父片段一到立即归位；超过可配置的最长等待时间仍等不到（或父编号根本不存在）则归到「父片段缺失」占位节点下，不影响其它片段入树。
3. **关键路径与耗时占比**：从根到最耗时叶子的路径、各服务在本次请求中的耗时占比。
4. **依赖图聚合与环检测**：持续从拼好的调用树提取「谁调用谁」，按最近 1 小时 / 1 天窗口聚合调用次数与平均耗时，Tarjan 算法标记成环边；切换窗口重新聚合，新旧窗口不串数据。
5. **入口基线与「这次 vs 平时」对照**：把请求按根片段的服务 + 入口标识（根片段上的 `operation`）归到各个入口，为每个入口增量维护两条基线——
   - 延迟基线：端到端耗时分布的 p50/p95/p99（P² 算法增量估计，单样本 O(1)，不重算历史）；
   - 结构基线：每条「谁调用谁」边在历史请求中的出现频率，达到门槛才算常规路径。

   每棵新拼好的树都先拿**并入之前**的基线快照做对照（超过 `p95 × 慢倍数` 判延迟劣化；比常规边多了/少了哪些边判结构漂移），对照结果落库后才把本次请求并入样本——异常不可能被自己稀释。样本数低于可配置门槛时对照结果一律标为「基线尚未成型」。无清晰根片段（全挂在占位节点下或根本没有根）的请求不归类、不入任何样本池。
6. **实时推送**：新片段入库、依赖图更新、入口基线/对照结果更新通过 SSE 推到前端。

前端是纯展示看板：请求检索列表 + 树状瀑布图（失败片段高亮、点击看详情）+ 服务依赖图（节点大小按频次、环边高亮、窗口切换）+ 入口对照（各入口样本量与基线分位、最近异常请求、点开看本次与基线的逐条对照）。

## 目录结构

```
backend/
  app/
    main.py            # ASGI 入口与生命周期
    config.py          # 环境变量配置（最长等待时间、DB 路径、基线门槛等）
    models.py          # 请求/响应 schema 与结构化错误
    validation.py      # 片段校验（脏数据拒绝）
    storage.py         # SQLite 持久化（片段 / 入口样本 / 排除名单 / 对照结果）
    tree.py            # 调用树拼接：挂起等待、占位节点、关键路径、耗时占比
    dependencies.py    # 依赖图聚合 + Tarjan 环检测
    entries.py         # 入口归类（根服务+operation）与样本特征归集
    latency.py         # 延迟基线：P² 增量分位（p50/p95/p99）
    structure.py       # 结构基线：调用边频率与常规边
    comparison.py      # 对照判定：延迟劣化 / 结构新增边 / 消失边 / 未成型
    baselines.py       # 基线协调器：先判定后并样本的次序、重启重建
    events.py          # SSE 实时事件总线
    service.py         # 门面：串联存储 / 拼接 / 聚合 / 基线
    api/
      __init__.py
      http.py          # HTTP 路由：上报、检索、树、依赖图、入口、对照详情
      push.py          # SSE 推送路由
      deps.py          # FastAPI 依赖注入
  tests/               # 自动化测试（乱序归位/去重/缺父不中断/顺序无关/环检测/脏数据拒绝/
                       #  窗口切换/增量分位/边频率/入口归类/延迟劣化/结构新增消失/
                       #  先判定后并样本/样本不足未成型/无根不入样/重启重建）
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
      EntryBrowser.jsx    # 入口浏览（样本量/基线分位/最近异常）
      ComparisonDetail.jsx # 单次请求与入口基线的对照详情
docker-compose.yml     # 后端(python:3.12-slim) + 前端构建(node:20-alpine) + nginx(alpine)
```

## 基线相关配置（环境变量）

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `TRACING_BASELINE_MIN_SAMPLES` | `20` | 入口最低样本数；不足时对照结果为「基线尚未成型」 |
| `TRACING_LATENCY_SLOW_FACTOR` | `1.5` | 端到端耗时超过基线 p95 × 该倍数判为延迟劣化 |
| `TRACING_STRUCTURE_EDGE_FREQUENCY` | `0.8` | 边出现频率达到该比例才算入口的常规路径 |

## 先判定后并样本的次序

每个入口的基线版本号等于该入口**并入下一个请求之前**的历史样本数。一棵新树到达后：

1. 取入口当前基线的只读快照（版本 = 当前样本数）；
2. 用该快照做延迟与结构对照，产出对照结果；
3. 对照结果（含所用版本号、分位、常规边快照）落库；
4. 才把本次请求并入样本池，基线版本 +1。

因此对照结果里记录的 `baseline_version` 永远不包含这次请求自身，重启后从持久化样本重建基线、对照结果原样可读回。

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
    "service": "gateway", "operation": "createOrder",
    "start_time": 1000, "end_time": 1300,
    "status_code": 200
  }]}'
```

根片段上的 `operation` 是入口标识（可缺省，归入默认操作）；入口归类依据为
「根片段所属服务 + operation」。响应中 `accepted` / `rejected` 分别列出
接受和拒绝的片段；被拒绝项带机器可读的 `code` 与字段级说明。
