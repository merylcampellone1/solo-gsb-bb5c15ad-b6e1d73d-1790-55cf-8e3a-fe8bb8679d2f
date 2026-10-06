# 回收分拣来料污染判定系统

提交批次检验数据（批次编号、来源区域、重量、物料类别与污染量），系统按**可版本化的接收规则**计算污染等级、处置结论、扣费明细与证据说明，并保存判定单与审计记录，支持查询与 CSV 导出。

## 功能一览

- **判定计算**：污染率 → 命中等级（A 合格 / B 轻度污染 / C 重度污染）→ 处置结论（合格接收 / 降级接收 / 拒收）→ 分类扣费明细与合计 → 中文证据说明
- **字段校验**：未知物料类别、负/零重量、负污染量、单项或总污染量超过批次重量，一律拒绝判定并返回 `422` 字段错误
- **规则版本化**：规则按 `(区域, 版本)` 存储，每区域一个启用版本，可新增版本、切换启用版本；判定单出具时保存**规则快照**，规则更新不会改写历史结论
- **判定修订**：发现已出具单据的称重或杂质录入有误时，凭原单号 + 所见最新单号提交复检重量与物料，系统沿用原单批次、区域与规则快照复算，生成关联前版的只读新版本；完整修订链可按编号追溯
- **判定复核**：质检员可对已出具的判定单选择同区域另一规则版本做只读复核：以原单保存的批次重量与物料明细按目标版本重新校验、计算，返回原结论与目标等级、扣费明细与差异项；不写单据、不产生修订与审计
- **费用结算**：操作员提交区域、当前有效（链尾）单号列表与幂等请求号，系统核对单号区域、币种与修订链状态后，在同一事务内冻结各链本次应结金额（与该链上次已结算版本的扣费差额，可为负）、来源版本与合计，生成不可修改的结算单并留审计；同一请求号重试安全，同一链同一版本只会入账一次
- **持久化与审计**：判定单、规则版本、结算单、审计日志存于 SQLite；判定创建、修订（含原因与操作者）、规则新增/切换、结算生成均留痕
- **页面与 API**：简洁 Web 页面（`/`）+ REST API（交互文档 `/docs`）+ CSV 导出

## 快速开始

### Docker Compose（一键启动）

```bash
docker compose up --build
```

访问 <http://localhost:8000>（页面）或 <http://localhost:8000/docs>（API 文档）。
数据保存在名为 `app-data` 的卷中（容器内 `/data/app.db`）。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `APP_DB` | `data/app.db`（容器内 `/data/app.db`） | SQLite 数据库文件路径 |

首次启动自动建表并写入种子规则：`default` 区域（v1.0 启用 / v1.1 备用）与 `华东` 区域（v1.0 启用）。种子不会覆盖已有数据，也不会重置你已切换的启用版本。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/assessments` | 提交判定，返回完整判定单 |
| GET | `/api/assessments` | 查询判定单（`region` / `level` / `batch_no` / `current` / `limit` / `offset`） |
| GET | `/api/assessments/{id}` | 判定单详情（含扣费明细、证据与修订链信息） |
| POST | `/api/assessments/{id}/revisions` | 修订判定单（沿用原单快照复算，生成新版本） |
| GET | `/api/assessments/{id}/revisions` | 查询完整修订链与当前有效（链尾）结论 |
| POST | `/api/assessments/{id}/review` | 复核判定单（按同区域目标规则版本重算对比，只读） |
| POST | `/api/settlements` | 结算所选修订链当前版本扣费，生成可追溯结算单（幂等） |
| GET | `/api/settlements` | 查询结算单列表（`region` / `limit` / `offset`） |
| GET | `/api/settlements/{id}` | 结算单详情（来源单号、原扣费、差额与合计） |
| GET | `/api/rules` | 列出全部规则版本 |
| GET | `/api/rules/{region}/active` | 查询区域当前启用规则 |
| POST | `/api/rules` | 新增规则版本（`activate: true` 可同时启用） |
| POST | `/api/rules/{region}/activate` | 切换区域启用版本 |
| GET | `/api/audit` | 查询审计记录 |
| GET | `/api/export/assessments.csv` | 导出判定单 CSV |
| GET | `/api/export/audit.csv` | 导出审计记录 CSV |
| GET | `/api/health` | 健康检查 |

## 调用示例

### 1. 提交判定

```bash
curl -X POST http://localhost:8000/api/assessments \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_no": "B20261005-001",
    "region": "default",
    "weight_kg": 100,
    "materials": [
      {"category": "PET", "contamination_kg": 3},
      {"category": "纸类", "contamination_kg": 2}
    ],
    "operator": "张三"
  }'
```

响应 `201`（节选）：

```json
{
  "id": "A0F9F9D8876",
  "rule_version": "v1.0",
  "contamination_ratio": 0.05,
  "level": {"code": "B", "name": "轻度污染"},
  "conclusion": "降级接收",
  "fee_lines": [
    {"category": "PET", "contamination_kg": 3.0, "fee_per_kg": 1.2, "multiplier": 1.0, "fee": 3.6},
    {"category": "纸类", "contamination_kg": 2.0, "fee_per_kg": 0.6, "multiplier": 1.0, "fee": 1.2}
  ],
  "total_fee": 4.8,
  "currency": "CNY",
  "evidence": [
    "依据规则版本 default/v1.0 判定。",
    "PET：污染 3kg × 费率 1.2 元/kg × 等级系数 1 = 3.60 元",
    "批次重量 100kg，污染总量 5kg，污染率 5.00%。",
    "污染率命中等级 B（轻度污染，阈值 ≤ 8%），处置结论：降级接收。"
  ]
}
```

### 2. 非法提交（字段错误）

```bash
curl -X POST http://localhost:8000/api/assessments \
  -H 'Content-Type: application/json' \
  -d '{"batch_no":"B002","region":"default","weight_kg":-1,
       "materials":[{"category":"锂电池","contamination_kg":3}]}'
```

响应 `422`：

```json
{
  "errors": [
    {"field": "weight_kg", "message": "批次重量必须为正数"},
    {"field": "materials[0].category", "message": "未知物料类别：锂电池，当前规则版本支持的类别：PET, HDPE, PP, 纸类, 金属, 玻璃, 织物, 其他"}
  ]
}
```

### 3. 切换区域规则版本（历史结论不受影响）

```bash
# 切换 default 区域到 v1.1（阈值更严、费率更高）
curl -X POST http://localhost:8000/api/rules/default/activate \
  -H 'Content-Type: application/json' \
  -d '{"version": "v1.1", "actor": "admin"}'

# 此后的新判定按 v1.1 计算；切换前出具的判定单仍固定为 v1.0 的结论与扣费
curl http://localhost:8000/api/assessments/A0F9F9D8876
```

### 4. 修订判定单（录入有误时复算）

修订需携带**所见最新单号** `base_id`（必须是修订链尾版）、修正原因、操作者，
以及复检后的重量与物料；批次、区域、规则快照沿用原单，区域规则切换不影响本次复算。

```bash
# 假设原单号为 A0F9F9D8876（当前链尾）
curl -X POST http://localhost:8000/api/assessments/A0F9F9D8876/revisions \
  -H 'Content-Type: application/json' \
  -d '{
    "base_id": "A0F9F9D8876",
    "weight_kg": 120,
    "materials": [{"category": "PET", "contamination_kg": 2}],
    "reason": "称重录入有误，按复检重量重算",
    "operator": "李四"
  }'
```

响应 `201`：生成关联前版的只读新单（`version: 2`，`parent_id` 指向前版，
`root_id` 指向原单），证据说明中注明修订来源与原因；原单内容不变，
仅 `is_current` 变为 `false`。每次成功修订都会把原因与操作者写入审计
（`assessment.revised`）。

```bash
# 查询完整修订链与当前有效结论（链内任意单号均可）
curl http://localhost:8000/api/assessments/A0F9F9D8876/revisions
```

```json
{
  "root_id": "A0F9F9D8876",
  "current_id": "A1B2C3D4E56",
  "current": {"id": "A1B2C3D4E56", "version": 2, "conclusion": "合格接收", "is_current": true},
  "items": [
    {"id": "A0F9F9D8876", "version": 1, "conclusion": "降级接收", "is_current": false},
    {"id": "A1B2C3D4E56", "version": 2, "conclusion": "合格接收", "is_current": true,
     "revision_reason": "称重录入有误，按复检重量重算", "operator": "李四"}
  ]
}
```

**并发与冲突**：只允许修订链尾。两个请求争用同一尾版时只接受一个，
其余返回 `409` 及明确提示，且不产生单据与审计记录：

```json
{"errors": [{"field": "base_id",
  "message": "修订冲突：所依据的单号已不是最新版本，当前链尾为 A1B2C3D4E56，请获取最新单号后重试"}]}
```

字段无效（`422`）、原单不存在（`404`）或校验失败时同样不产生单据和审计记录。

### 5. 新增规则版本

```bash
curl -X POST http://localhost:8000/api/rules \
  -H 'Content-Type: application/json' \
  -d '{
    "region": "华南", "version": "v1.0", "activate": true,
    "categories": {"PET": {"fee_per_kg": 1.0, "desc": "PET 瓶类"}},
    "levels": [
      {"code": "A", "name": "合格", "max_ratio": 0.05, "conclusion": "合格接收", "fee_multiplier": 0},
      {"code": "B", "name": "超标", "max_ratio": 1.0,  "conclusion": "拒收",     "fee_multiplier": 1.0}
    ]
  }'
```

### 6. 查询与导出

```bash
curl 'http://localhost:8000/api/assessments?region=default&level=B'
curl 'http://localhost:8000/api/assessments?current=true'   # 只看各修订链的当前有效版本
curl 'http://localhost:8000/api/audit?entity=rule'
curl -OJ http://localhost:8000/api/export/assessments.csv
curl -OJ http://localhost:8000/api/export/audit.csv
```

### 7. 复核判定单（换规则版本重算对比）

质检员选择一张已出具的判定单与同区域一个已存在的规则版本，系统以原单保存的
批次重量、物料明细按目标版本重新校验、计算，返回原结论与目标等级、扣费明细、
差异项及双方规则版本。**复核只读**：不改变原单、修订链或审计记录。

```bash
# 假设原单号为 A0F9F9D8876（default 区域，出具时按 v1.0 判定）
curl -X POST http://localhost:8000/api/assessments/A0F9F9D8876/review \
  -H 'Content-Type: application/json' \
  -d '{"rule_version": "v1.1"}'
```

响应 `200`（节选）：`original` 为原单结论，`review` 为按目标版本重算的结果，
`diff` 为差异项（等级/结论是否变化、扣费差额、分类费率对比与中文说明）。

```json
{
  "assessment_id": "A0F9F9D8876",
  "batch_no": "B20261005-001",
  "region": "default",
  "weight_kg": 100,
  "materials": [{"category": "PET", "contamination_kg": 3}],
  "original": {"rule_version": "v1.0", "level": {"code": "B", "name": "轻度污染"},
               "conclusion": "降级接收", "total_fee": 3.6},
  "review": {"rule_version": "v1.1", "level": {"code": "B", "name": "轻度污染"},
             "conclusion": "降级接收", "total_fee": 4.5},
  "diff": {
    "level_changed": false,
    "conclusion_changed": false,
    "total_fee_delta": 0.9,
    "fee_line_diffs": [
      {"category": "PET", "original_fee_per_kg": 1.2, "review_fee_per_kg": 1.5,
       "original_fee": 3.6, "review_fee": 4.5, "delta": 0.9}
    ],
    "items": ["等级不变：B（轻度污染）。", "扣费合计 3.60 → 4.50 元（+0.90）。"]
  }
}
```

拒绝情形：判定单不存在（`404`）、目标规则版本不存在（`404`）、`region`
与原单区域不一致（`422`）、原单缺少可重算的批次重量或物料明细（`422`）；
目标版本重算仍执行现有重量、类别与污染量校验，不通过时返回 `422` 字段错误。
以上失败与成功复核一样，均不产生任何单据、修订或审计记录。

### 8. 结算判定扣费（生成可追溯账单）

操作员提交**区域、当前有效（链尾）单号列表与幂等请求号**，系统核对各单号
所属区域、币种一致与修订链状态后，在同一事务内冻结每条链本次应结金额、
来源版本与合计，生成不可修改的结算单并写入审计（`settlement.created`）。

```bash
# 结算 default 区域两张当前有效判定单
curl -X POST http://localhost:8000/api/settlements \
  -H 'Content-Type: application/json' \
  -d '{
    "region": "default",
    "assessment_ids": ["A0F9F9D8876", "A1B2C3D4E56"],
    "request_id": "REQ-20261006-001",
    "operator": "财务-王五"
  }'
```

响应 `201`（节选）：每条链一项，记录来源单号、来源版本、该版本原扣费、
与上次已结算版本的差额及本单合计。

```json
{
  "id": "S7E8F9A0B12",
  "request_id": "REQ-20261006-001",
  "region": "default",
  "currency": "CNY",
  "total_delta": 8.4,
  "items": [
    {"root_id": "A0F9F9D8876", "assessment_id": "A0F9F9D8876", "version": 1,
     "original_fee": 3.6, "prev_settled_id": null, "prev_fee": null, "delta": 3.6},
    {"root_id": "A1B2C3D4E56", "assessment_id": "A1B2C3D4E56", "version": 1,
     "original_fee": 4.8, "prev_settled_id": null, "prev_fee": null, "delta": 4.8}
  ]
}
```

**幂等重试**：同一 `request_id` 与相同内容重发返回 `200` 与原结算单，
不重复入账、不重复审计；内容不同则返回 `409` 冲突。

```bash
# 已结算的链仍可正常修订；之后用新链尾再次结算时，
# 仅计入新链尾与该链上次已结算版本的扣费差额（可为负），旧结算单不变
curl -X POST http://localhost:8000/api/assessments/A0F9F9D8876/revisions \
  -H 'Content-Type: application/json' \
  -d '{"base_id": "A0F9F9D8876", "weight_kg": 120,
       "materials": [{"category": "PET", "contamination_kg": 2}],
       "reason": "称重录入有误，按复检重量重算", "operator": "李四"}'
# 假设新链尾为 A2C3D4E5F67（扣费 0），再次结算得 delta = 0 - 3.6 = -3.6
curl -X POST http://localhost:8000/api/settlements \
  -H 'Content-Type: application/json' \
  -d '{"region": "default", "assessment_ids": ["A2C3D4E5F67"],
       "request_id": "REQ-20261006-002", "operator": "财务-王五"}'

# 查询结算单：列表 + 详情（来源单号、原扣费、差额及合计）
curl 'http://localhost:8000/api/settlements?region=default'
curl http://localhost:8000/api/settlements/S7E8F9A0B12
```

拒绝情形（均不生成结算单与审计）：单号列表为空或含重复（`422`）、
单号不存在（`404`）、单号非链尾（`422`）、单号区域与请求区域不一致（`422`）、
各链币种不一致（`422`）、同一链当前版本已结算（`409`）。

## 费用结算设计

- 结算单（`settlements`）与结算明细（`settlement_items`）只增不改：
  无任何更新接口，生成后不可修改，旧结算单永远保持原样。
- 请求必须携带区域、当前有效（链尾）单号列表与幂等请求号 `request_id`；
  服务端在**单个持锁事务**内完成全部核对（单号存在、为链尾、区域一致、
  币种一致、该版本未结算）并冻结各链本次应结金额、来源版本与合计。
- 每条链的本次应结金额 = 新链尾扣费 − 该链**上次已结算版本**扣费
  （首次结算时上次版本为空，差额即全额）；差额可为负，实现多退少补，
  且已入账金额不会重复计入。
- 幂等：`request_id` 唯一；同一请求号 + 相同内容（区域、排序后的单号
  列表、操作者的内容指纹）重试返回原单（`200`），内容不同返回 `409`。
- 并发：`settlement_items` 上 `UNIQUE(root_id, assessment_id)` 约束 +
  事务内复核，保证同一链同一版本只会入账一次；并发争用时只有一个请求
  成功（`201`），其余收到 `409`。
- 已结算链仍可按原有规则修订；修订不影响已生成的结算单，下次结算
  新链尾时自动按差额入账。
- 结算成功写入审计 `settlement.created`（操作者、请求号、区域、币种、
  合计与各链差额）；任何校验失败、冲突都不产生结算单与审计记录，
  且失败不占用请求号，修正后可重新提交。

## 规则版本化设计

- 规则以 `(region, version)` 唯一存储，每个区域恰好一个 `active` 版本。
- 出具判定时将**规则快照**与请求、结果一并写入判定单，历史单据永不重算。
- 切换启用版本只影响之后的新判定；也可在提交时显式指定 `rule_version` 复用旧版本。
- 判定创建、规则新增、规则切换均写入审计日志（操作者、动作、对象、明细、时间）。

## 判定修订设计

- 每张判定单属于一条**修订链**：原单为 `v1`（`root_id` 指向自身），每次修订生成
  `v2`、`v3`…，`parent_id` 依次串联，所有版本只读、永不改写。
- 修订**沿用原单的批次、区域与规则快照**重新校验、计算，区域规则切换不影响复算结果。
- 提交必须携带所见最新单号 `base_id`；服务端在单个事务内复核 `base_id` 仍为链尾后
  才插入新版本，因此并发争用同一尾版时只有一个请求成功，其余收到 `409` 冲突。
- 校验失败、单号不存在或冲突时不产生任何单据与审计记录；成功修订以
  `assessment.revised` 写入审计，明细含原因、操作者、版本与复算结论。
- 详情、列表与 CSV 在既有字段之后追加 `version` / `is_current` / `root_id` /
  `parent_id` / `revision_reason`，既有字段含义不变；`GET /api/assessments/{id}/revisions`
  返回完整修订链与链尾（当前有效）结论。

## 判定复核设计

- 复核是**纯只读**的对比计算：读取原单保存的批次重量与物料明细，按同区域
  目标规则版本重新执行与出具判定相同的校验与计算，不写任何表、不留审计。
- 目标版本必须与原单同区域：请求可显式携带 `region`（缺省取原单区域），
  与原单区域不一致直接 `422`；版本不存在 `404`。
- 原单缺少可重算明细（历史遗留数据缺批次重量或物料明细）时 `422` 拒绝；
  目标版本下类别不再受支持等校验失败同样 `422`，且无任何副作用。
- 响应同时给出 `original`（原单结论）、`review`（目标版本重算结果，含证据）
  与 `diff`（等级/结论变化标记、扣费总差额、分类费率与扣费对比、中文差异说明），
  便于质检员直接核对规则调整对历史批次的影响。

## 项目结构

```
app/
  main.py            # FastAPI 入口：API 路由、错误处理、CSV 导出
  rules.py           # 规则引擎：种子规则、提交校验、判定计算
  storage.py         # SQLite 持久化：判定单（含修订链）/ 规则版本 / 结算单 / 审计日志
  static/index.html  # 单文件 Web 页面
tests/test_api.py    # 53 个接口、版本化、修订、复核与结算行为测试
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
