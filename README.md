# OCR 评测清单 API

同一页手稿补扫会产生多个扫描件号。本服务用于管理 OCR 评测清单，确保评测件号
不与训练用过的原页（直接或间接同页）重复：登记训练件号与同页判定，提交评测
清单时校验冲突，已发布清单出现冲突即暂停供数，消除后按原编号恢复。

技术栈：FastAPI + SQLite（WAL），docker compose 一键启动。

## 快速开始

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，文档见 http://localhost:8000/docs
```

本地开发：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload          # 数据库默认 ./ocr_eval.db，可用 DB_PATH 覆盖
python -m pytest tests/                # 运行测试（需额外安装 pytest httpx）
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/training-scans` | 登记曾用于训练的扫描件号（幂等） |
| DELETE | `/training-scans/{scan_id}` | 注销训练件登记，相关冲突随之消除 |
| GET | `/training-scans` | 列出已登记训练件号 |
| POST | `/same-page-links` | 按证据号登记两件同页的判定（间接相连也算同页） |
| DELETE | `/same-page-links/{evidence_id}` | 按证据号撤回同页判定（幂等） |
| GET | `/same-page-links` | 列出全部同页判定（含已撤回） |
| POST | `/manifests` | 提交评测件号清单：有冲突退回（409），无冲突发布并发清单号（201） |
| GET | `/manifests/{manifest_id}` | 凭清单号查询：无冲突供出原清单；有冲突只返回冲突的评测件号与训练件号 |
| POST | `/sampling-plans` | 传 2~6 个不同且正常供数的清单号生成抽页方案，整份落库后发方案编号 |
| GET | `/sampling-plans/{plan_id}` | 凭方案编号查询当初选中的清单号与件号，并复核方案当前是否可用 |

### 典型流程

```bash
# 1. 登记训练件号
curl -X POST localhost:8000/training-scans -H 'Content-Type: application/json' \
     -d '{"scan_id": "T-001"}'

# 2. 按证据号登记同页判定（同一页手稿的多个补扫件号）
curl -X POST localhost:8000/same-page-links -H 'Content-Type: application/json' \
     -d '{"evidence_id": "E-1", "scan_a": "T-001", "scan_b": "P-100"}'

# 3. 提交清单：含同页件号即退回，返回冲突明细
curl -X POST localhost:8000/manifests -H 'Content-Type: application/json' \
     -d '{"items": ["P-100", "P-200"]}'
# 409 {"detail": "...已退回", "conflicts": [{"eval_scan_id": "P-100", "training_scan_id": "T-001"}]}

# 4. 无冲突则发布并发清单号
curl -X POST localhost:8000/manifests -H 'Content-Type: application/json' \
     -d '{"items": ["P-300"]}'
# 201 {"manifest_id": "MFT-XXXXXXXXXXXX", "status": "published", "items": ["P-300"]}

# 5. 凭清单号查询
curl localhost:8000/manifests/MFT-XXXXXXXXXXXX
# 无冲突: {"status": "published", "items": [...]}
# 有冲突: {"status": "suspended", "conflicts": [{"eval_scan_id": ..., "training_scan_id": ...}]}

# 6. 撤回误判的同页判定 / 注销训练登记后，清单自动按原编号恢复供数
curl -X DELETE localhost:8000/same-page-links/E-1
```

## 抽页方案（多批共用人工转写原文）

几批 OCR 评测共用人工转写原文时，按「原页」抽页：原页即同页连通分量，
在至少两批清单中出现的原页为共用页。

```bash
# 传 2~6 个不同且正常供数的清单号
curl -X POST localhost:8000/sampling-plans -H 'Content-Type: application/json' \
     -d '{"manifest_ids": ["MFT-AAA", "MFT-BBB", "MFT-CCC"]}'
# 201：
# {
#   "plan_id": "PLN-XXXXXXXXXXXX",
#   "status": "ready",
#   "manifest_ids": ["MFT-AAA", "MFT-BBB", "MFT-CCC"],
#   "pages": [
#     {"page_index": 0, "component_id": 7,
#      "picks": [{"manifest_id": "MFT-AAA", "scan_id": "A1"},
#                {"manifest_id": "MFT-BBB", "scan_id": "B1"}]},
#     ...
#   ]
# }
```

规则与返回码：

- **入参**：2~6 个互不相同的清单号（422）；清单号不存在 404；
  任一清单暂停供数 409（`suspended_manifest_ids`）。
- **共用页上限 14**：在至少两批出现的原页超过 14 页时返回 400，
  提示缩小范围（`shared_page_count` / `limit`），不落任何方案。
- **最少选页连通**：以「一页覆盖的批次」为超边，求把任意两批直接或间接
  连起来的最少超边集；同规模取字典序最小者，结果确定。无法连通时返回 409
  与各自连通的批次组 `groups`，同样不落方案。
- **按页取件并固化**：选出的每一页在含它的各批各取一件（同页多件取件号
  最小者），按 `(方案, 页, 清单号)` 保存清单号与件号；整份方案在单事务内
  写好后才发方案编号。
- **凭编号查询**：`GET /sampling-plans/{plan_id}` 始终能查到当初选中的
  件号；同时以当前一份已提交资料复核，出现任一情形则 `status` 置为
  `unavailable` 并给出 `unavailable_reasons`：
  - `manifest_suspended`：来源清单暂停供数；
  - `picks_page_split`：同页同批固化的件号现已分属不同原页；
  - `pages_merged`：方案中两组合到了同一原页。
  情形消除后查询自动恢复 `ready`，固化快照不变。

## 设计说明

- **同页关系增量物化**：`scan_components` 表维护每个件号所属的连通分量。
  登记判定时合并分量；撤回判定时仅在受影响分量内做 BFS 判断是否分裂。
  按清单号查询只做 SELECT，无需重新串联同页判定。
- **冲突增量物化**：`conflicts` 表维护 `(清单号, 评测件号, 训练件号)` 三元组，
  始终精确等于「清单内评测件所在分量含训练件」的全部组合。清单供数状态由
  该表派生：有冲突即暂停供数，只返回冲突对；冲突清空即按原编号供出原清单。
- **事务原子性**：训练登记、同页判定的变动与供数结论（conflicts 表）在同一
  SQLite 事务（`BEGIN IMMEDIATE`）内提交；任一步失败整体回滚，沿用原状态。
- **幂等性**：重复登记训练件 / 同一判定返回 200 不产生副作用；重复撤回判定
  幂等；已撤回的证据号可重新登记同一对件号；证据号不得改指其他件号对（409）。
- **抽页方案物化**：`sampling_plans` / `sampling_plan_manifests` /
  `sampling_plan_pages` / `sampling_plan_picks` 四表固化方案入参、选页与
  每页各批选中的件号。原页直接复用 `scan_components` 连通分量，无需重新
  串联同页判定；方案在单事务内整份落库后才发编号，失败不留残留。查询只读
  当前已提交资料做可用性复核，不改动固化快照。

## 目录结构

```
app/
  main.py    # FastAPI 路由
  store.py   # 领域逻辑：分量维护、冲突物化、清单发布、抽页方案
  db.py      # SQLite 连接、schema、写事务
tests/test_api.py
tests/test_sampling.py
Dockerfile / docker-compose.yml
```
