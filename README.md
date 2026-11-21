# 时档（HourSpan）

**骨架：** 门店营业属性贴源入档 → 业务键变更检测 → Type2 历史行开闭 → 当前镜投影 → 历史与当前互证

- 领域：数据工程（去皮=维表 SCD / 历史开闭 / 当前镜 / 核证；不是门店后台 CRUD）
- 端口：前端 **6200**，接口 **11200**（避开 Chrome 封锁的 6000–6063）
- 公有仓前缀：`lza0829/hourspan-NN-*`
- 栈：Python 3.12 + FastAPI + SQLite；前端 **React + Vite**
- 避让：勿撞筑层分层汇总、价舱改价调度、信汇多源去重、变流 CDC 游标、格湾占用态

本地冒烟：

```powershell
cd claude-workspace/hourspan/_scaffold/backend
pip install -r requirements.txt
$env:DATA_DIR="$PWD/data"; $env:FIXTURES_DIR="$PWD/../fixtures"
python -m uvicorn app.main:app --host 127.0.0.1 --port 11200

cd ../frontend
npm install
npm run dev
```

或：`docker compose up -d --build`（6200 / 11200）

## 主链路：草稿切换（draft_then_commit_switch）

检测应用不再直改正式。拟关旧 / 拟开新先入**草稿区**，与现行 `is_current` 与区间规则**对撞**，
通过后才在单个事务内**原子切正式**（关旧 + 开新 + 换镜）。

**落库架构（全局唯一，选定「甲 · 失败草稿留痕」）**

| 项 | 甲（选定） | 乙（已否决） |
| --- | --- | --- |
| 对撞失败 | 正式态完全不动；草稿保留 `failed` + `fail_reason` | 连草稿一并回滚删除 |
| 失败原因可查 | 核证只读接口直接返回失败草稿 | 只能查独立切换审计，核证看不到残留草稿 |

**硬约束**

- 禁止无对撞直接改正式：`POST /api/scd/apply` → `409 direct_formal_write_blocked`。
- 禁止失败后双当前 / 半截切换：提交前后均自检 `is_current` 唯一、区间连续、镜与当前行互证；
  任一不满足整票回滚，正式原样。
- 邻键草稿不串切本键：对撞与提交均按 `store_id` 锁定草稿。
- 未提交草稿（`draft` / `validated`）存在时，后到应用一律 `pending_draft_exists` 拒绝，不得并行切正式。
- 提交阶段异常也整票回滚，并在新事务补记 `failed` 草稿 + 审计，保证失败可查。

**接口**

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/api/switch/apply` | 草稿 → 对撞 → 原子提交；`prepare_only=true` 只落草稿 |
| POST | `/api/switch/commit` | 提交 `prepared` 跑次（事务内重对撞） |
| POST | `/api/switch/abandon` | 放弃未提交跑次 |
| GET | `/api/switch/proof` | 只读核证：run、草稿态、正式态、区间校验、valid |
| GET | `/api/switch/drafts` · `/api/switch/events` | 草稿区 / 切换审计 |

**测试**

```bash
cd backend && python -m pytest tests -q
```

覆盖：成功切换；故意对撞失败（正式原样 + 失败草稿可见 + 另跑次草稿空）；半截切换夹具正式原样；
未提交草稿阻塞后到应用；邻键隔离；直改写闸；重复应用拒绝；提交阶段回滚留痕。

