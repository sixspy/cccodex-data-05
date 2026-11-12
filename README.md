# cc-data-lab

CC_Codex 二期（26.11）**数据工程**主线基线：**采集任务控制台**（Flask 后端 + HTMX 前端页 + 站内 Mock 目录）。

## 领域

- **所属领域**：数据工程  
- **子方向**：数据采集、爬虫与 RPA  

## 技术栈

- Python 3.12+、Flask 3、SQLite  
- HTMX（CDN）+ Jinja 模板  
- pytest  

## 本地运行

```powershell
cd claude-workspace\cc-data-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

浏览器打开 http://127.0.0.1:5590 ，登录 **admin / 123456**。

流程：创建任务 → 选种子 `/mock/catalog/alpha` 或 `beta` → 详情页「运行采集」→ 查看落库（alpha 含重复标题，会演示去重跳过）。

## 每日入库配额与排队

每个站内目录（种子路径）都有**每日成功入库配额**，在列表页「每日配额」一栏填写，留空或未配置按 **5 条**。

- **日期由服务端决定**：页面顶部显示「服务端日期」，配额按这个日期统计（`APP_TIMEZONE` 可指定口径）。浏览器提交的任何日期字段都不参与判定。验收时可用 `SERVER_DATE_OVERRIDE=2026-11-12` 固定日期，测试里通过 `TestConfig.SERVER_DATE_OVERRIDE` 固定或推进。
- **超额只排队**：当天配额内、编码非空的合法条目直接进正式结果（`crawl_records`）；超出配额的条目只进入 `queue_entries`，保留入队先后，不进正式结果。
- **换日不会自动入库**：日期变化后队列仍是 `pending`，必须手工点详情页的「继续采集」。继续时仍按**新一天**的配额放行，超出部分继续排队。
- **插队**：每个任务同一时刻只允许一条插队，且必须填写原因；任务里已有插队时再登记会被拒绝。编码为空的条目不能插队，也不占配额（入库前被标记 `invalid`）。
- **继续顺序**：下一次继续先入库插队的那一条，再按排队顺序（自增 `id` 升序）处理其余条目。
- **运行互斥**：任务处于 `running` 时再次触发会被拒绝（路由层提示「本次运行尚未结束」）。

### 浏览器里怎么看排队顺序

列表页与详情页都会显示：**目录今日已入库 / 配额 / 排队条数 / 当前有无插队**。

详情页「待处理队列」表格的 **「排队顺序」列**就是放行次序：

1. 插队条目固定显示为 **1**，并带蓝色底色，`插队` 列写明原因；
2. 普通条目按数字递增（2、3、4…）排列，数字越小越先入库；
3. `状态` 列 `pending` = 仍在排队，`admitted` = 已进正式结果，`invalid` = 空编码被作废，`duplicate` = 命中任务内去重。

每次点「继续采集」后刷新页面，`排队顺序` 会从剩下的条目重新从 1 编号，`状态` 与 `入库日期` 随之更新，据此即可核对“插队先入库、再按排队顺序”的实际效果。

### 配额 / 插队测试

`tests/test_quota_queue.py` 用 `TestConfig.SERVER_DATE_OVERRIDE` 固定服务端日期，覆盖：

1. 超额之后只排队、不进正式结果；
2. 同一天反复继续也不超过配额；
3. 换日之后队列不自动入库，手工继续仍受新一天配额限制；
4. 插队先于普通排队入库；
5. 空编码不能插队、也不占配额；
6. 已有插队时再插一条被拒绝；
7. 运行尚未结束时拒绝再次触发。

另有页面渲染断言，确认列表/详情显示配额、排队与插队状态。运行：`pytest`。

## Docker 一键启动（推荐用于快照前验收）

需已打开 **Docker Desktop**：

```powershell
cd claude-workspace\cc-data-lab
docker compose up -d --build
docker compose ps
```

- 页面：http://127.0.0.1:5590/login  
- Mock：http://127.0.0.1:5590/mock/catalog/alpha  
- 停止：`docker compose down`（保留库卷）· 清卷：`docker compose down -v`

`docker compose ps` 中 `STATUS` 含 **healthy** 表示服务可访问。线下表「环境可复现等级」可填 **已容器化，可一键起环境**。

## 前后端说明

| 层 | 说明 |
|----|------|
| 后端 | `app/` 路由、任务状态机、同步 runner、SQLite |
| 前端 | `templates/` + `static/`，浏览器可点操作 |
| Mock 源站 | `/mock/catalog/<id>` HTML，无需外网 |

## 初始快照

在首次 Claude 单轮跑题前 commit + push 本树（不含 `.venv/`、`instance/*.db`）。后续题目在同 repo 上叠加调度、RPA 步骤、增量水位等能力。

## 基线 vs 跑题目录

| 路径 | 角色 |
|------|------|
| `claude-workspace/cc-data-lab`（本目录） | **基线**：改功能、docker 验收 |
| `ccproject/cc-data-lab/data-XX` | **单题**：复制基线后挂容器、出快照 |

复制：`..\scripts\Copy-BaselineToQuestion.ps1 -QuestionId data-01`

## 跑题 Harness

见 `CCcodex/docs/ClaudeCode_Windows使用说明.md`（镜像 2.0）。容器挂载 **ccproject 下该题目录**，不是本基线路径。
