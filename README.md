# 期刊补缺核对 API

给一卷旧期刊补缺前，把 **有藏 / 缺刊 / 待翻册** 分清楚。FastAPI + SQLite，卷清单最多 24 期，按条码成批增补或更正馆藏，每册最多三组候选期次（用于封面模糊的合刊）。

## 快速启动

```bash
docker compose up --build
```

- Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>
- 健康检查: <http://localhost:8000/healthz>
- 数据卷：命名卷 `periodical-data`，SQLite 文件在容器的 `/data/periodicals.sqlite3`

本地开发（无需 Docker）：

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
PERIODICALS_DB=./data/periodicals.sqlite3 uvicorn app.main:app --reload
python3 scripts/demo.py            # 或：API_BASE=http://127.0.0.1:8000 python3 scripts/demo.py
pytest                             # 82 项测试
```

## 核心规则

| 规则 | 实现 |
| --- | --- |
| 一卷最多 24 期 | 建卷 `issues` 长度校验（1–24），求解器硬上限 `MAX_ISSUES=24` |
| 每册最多三组候选期次 | 请求校验 `candidates` 长度 ≤ 3；库中 `candidate_groups.position` 只允许 0–2 |
| 每组内期次一并计入 | 候选组是期次位掩码，求解时整组占用 |
| 每册必须选定一组 | 求解要求每册恰好选一个候选组，不能跳过 |
| 同一期最多归一册 | 所有被选候选组两两不相交 |
| 有藏 / 缺刊 / 待翻册 | 对**全部合法方案**求交/并：都覆盖→`held`；都未覆盖→`missing`；其余→`review` |
| 整批核对 | 每批与库中其余馆藏合并后统一求解，不是逐条判断 |
| 无解整批退回 | 求解失败时不写任何 `copies/candidate_*`，只写一条 `rejected` 批次记录；原馆藏与缺刊结论不变 |
| 并发以实际入库顺序为准 | `BEGIN IMMEDIATE` 先取写锁再读馆藏，提交串行化，`batches.seq` 即真实提交顺序 |
| 读取取自同一已提交时点 | `GET /volumes/{id}`、`/gaps`、`/batches` 全程包在一个只读快照事务（WAL 延迟 `BEGIN`）里：卷清单、馆藏、候选组、有藏/缺刊/待翻册报告来自同一提交点，批次在请求中途提交也不会拼出数据库从未存在过的状态 |
| 原馆藏和缺刊结论仍可查 | 退批次保留 `reason`、请求快照、提交前后对照；`GET /volumes/{id}/gaps` 始终可查 |

求解算法见 `app/solver.py`：对最多 `2^24` 个已用期次状态做位集 DP，每册做 `reachable ∩ free(group)` 再按组位左移；复杂度约为 `O(册数 × 候选组数 × 2^n / 机器字长)`，24 期稠密用例实测 < 0.3 s。正确性由 60 组随机用例与暴力全枚举对拍（`tests/test_solver.py`）。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/volumes` | 建卷：`title`、`volume_label`、`issues`（1–24 个有序期次名） |
| GET | `/volumes/{id}` | 卷清单、当前馆藏、实时缺刊报告 |
| GET | `/volumes/{id}/gaps` | 仅缺刊报告：`held` / `missing` / `review` + 中文标签 |
| POST | `/volumes/{id}/batches` | 提交一批增补/更正；`preview=true` 只试算不落库 |
| GET | `/volumes/{id}/batches` | 批次审计，按实际提交顺序 |
| GET | `/volumes/{id}/batches/{batch_id}` | 单批详情（含提交前后对照与变化明细） |

批次请求体：

```json
{
  "items": [
    {"barcode": "BC-001", "action": "add",    "candidates": [["第1期", "第2期"], ["第3期", "第4期"]]},
    {"barcode": "BC-009", "action": "update", "candidates": [["第3期"]]}
  ],
  "preview": false
}
```

`action=add` 要求条码在该卷尚不存在；`action=update` 用新候选组整体替换旧候选组（可借此更正旧的模糊判断）。同一批内条码不可重复，条码全局唯一（不能同时属于两卷）。

无解时返回 **201**，响应体 `status="rejected"` 并给出 `reason`；请求本身的格式/引用错误返回 4xx（`unknown_issue`、`too_many_candidates`、`barcode_exists` 等）。

## Swagger 演示：新册排除旧候选后，缺刊结论怎样变化

1. `POST /volumes` 建 4 期卷（`["第1期","第2期","第3期","第4期"]`）。
2. `POST /volumes/{id}/batches` 加合刊册 `BC-001`，候选组 `[["第1期","第2期"], ["第3期","第4期"]]` → 四期全是 **待翻册**。
3. `POST /volumes/{id}/batches` 加 `BC-002`，候选 `[["第3期"]]`，**`preview=true`** → `would_commit`，`reconciliation.after` 变成：第1–3期 **有藏**，第4期 **缺刊**。原因：BC-002 占了第3期，BC-001 只剩 `[1,2]` 一种合法选择，第4期不再被任何合法方案覆盖。
4. 去掉 `preview` 再提交一次 → 结论落库。
5. 再提交 `BC-003` 候选 `[["第1期","第2期"]]` → 与 BC-001 冲突，`status="rejected"`；`GET /gaps` 仍是「1–3 有藏、4 缺刊」，`GET /batches` 能看到这条退回记录。

`python3 scripts/demo.py` 会把上述流程连同并发提交、以及「读取与更正交错」一起跑一遍并打印结果：读取线程在更正批次提交前后反复调用 `GET /volumes/{id}` 与 `/gaps`，观测到的状态只可能是更正前或更正后两个完整提交状态之一（脚本内置断言，拼合状态会直接失败）。

### Swagger 手动演示交错读取与更正

1. 建卷：`POST /volumes`，3 期 `["第1期","第2期","第3期"]`，记下 `volume_id`。
2. 加初始馆藏：`POST /volumes/{id}/batches`，`D-1` 候选 `[["第1期"]]` → 第1期有藏。
3. 打开两个 Swagger 标签页（或两个 `curl` 终端）：
   - 标签页 A 反复执行 `GET /volumes/{id}` 与 `GET /volumes/{id}/gaps`；
   - 标签页 B 执行更正批次：`D-1` 改为 `[["第2期"]]` 并新增 `D-2` 候选 `[["第3期"]]`。
4. A 在 B 提交前拿到的响应始终是「仅 D-1、第1期有藏」；提交后拿到的始终是「D-1+D-2、第2/3期有藏、第1期缺刊」。**不会**出现「D-2 已在馆藏里、但报告仍按旧候选组计算」这类前后拼接的结论。
5. `GET /volumes/{id}/batches` 中 `seq` 即实际入库顺序；再提交一个与现有馆藏冲突的批次可看到 `status="rejected"`，原结论保持不变。

## 目录

```
app/main.py      FastAPI 路由与 Swagger 文档
app/service.py   卷/批次校验、事务、审计、缺刊报告
app/solver.py    位集 DP 精确求解器（有藏/缺刊/待翻册）
app/db.py        SQLite 连接、WAL、schema
app/schemas.py   Pydantic 请求模型
tests/           82 项测试（求解器对拍 + API/事务/并发 + 快照一致性）
scripts/demo.py  演示脚本
```

## 待办 / 已知限制

- **当前环境没有 Docker**（无 `docker`/`dockerd`，非 root 无法安装），`Dockerfile` 与 `docker-compose.yml` 已按标准写法交付但**未在本机实际构建**；服务本身用 `uvicorn` 直接启动验证过。请在有 Docker 的机器上跑一次 `docker compose up --build` 确认镜像可构建。
- SQLite 是单写者模型：并发提交会串行排队（`busy_timeout=30s`）。一期一馆的量级足够；若要多实例横向扩展需换 PostgreSQL 并保留同样的 `SERIALIZABLE`/行锁语义。
- 批次是「整批要么全提交、要么全退回」；暂不支持部分接受。若需部分接受，应先拆批再提交。
- 未做鉴权与操作人记录。生产部署前应加认证，并把 `batches` 增加 `operator` 字段。
- `preview` 在只读快照事务里执行并在结束时回滚；普通 GET 也使用同样的只读快照事务（`db.read_snapshot`），读事务不持有写锁，不阻塞并发批次提交，只保证单次请求内各表读到同一提交点。
- WAL 下长读事务会阻止旧 WAL 帧被检查点回收；本应用的请求都是短事务，无影响。若未来新增长查询，需单独评估。
- 缺刊报告只依据「候选组合法分配」，不校验物理册数上限（例如同一条码的复本）；如馆藏存在复本，需要先按册登记不同条码。
