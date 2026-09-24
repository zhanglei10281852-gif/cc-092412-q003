# 乡镇政务协同服务

这是一个面向乡镇综合服务中心的模块化后端，集中管理居民档案、政务事务、信访流转、公告、部门、用户、角色、权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 居民档案：登记、查询、更新和关联事务。
- 事务办理：受理、分派、退回、办结和部门责任查询。
- 职责分离：可按事务类别配置办结前的经办/复核分离，支持复核轮次、实时资格复检、防重复决定和完整决定历史。
- 信访流转：签收、分派、办理、审核、复查、催办和流转记录。
- 公告与部门：公告置顶、分类检索、部门信息及关联业务查看。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、居民事务、信访状态流转、公告排序、后台任务去重与领取，以及数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         居民、事务、公告、部门和信访业务接口
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 职责分离（事务经办与复核）

民政补助类等事务可按类别启用职责分离，默认受控类别为“低保”。受控类别在办结前必须由**不同人员**分别完成经办与复核，两人都要具备承办部门的当前数据范围。

- `GET /api/affairs/review-policies`：查看各类别策略。
- `PUT /api/affairs/review-policies/{类别}`：配置是否受控（需 `affairs.policy.write` 权限）。
- `POST /api/affairs/{id}/submit`：经办人提交办理结果，进入“待复核”并开启一个复核轮次（需 `affairs.write`）。
- `POST /api/affairs/{id}/review`：复核人通过或退回（需 `affairs.review`，且不得是本轮经办人）。
- `GET /api/affairs/{id}` 与 `GET /affairs/{id}`：返回 `current_responsibility`（当前责任岗位与待办原因）、`review_rounds`（每一轮经办/复核记录）和 `decisions`（不可变决定历史）。

规则要点：

- 复核决定作出时会对经办、复核双方的账号状态、权限和部门任期做**实时复检**，任一方失效都会阻断尚未完成的审批，事务停留在“待复核”。
- 重复点击受状态机、`(事务,轮次,阶段)` 唯一约束和 `Idempotency-Key` 请求头三重保护，只形成一次有效决定；同一幂等键的重试返回 `idempotent_replayed=true`。
- 复核退回后沿用原有“已退回 → 待受理 → 办理中”的重办流转；重新提交会开启**新的复核轮次**，旧轮次的决定证据完整保留。
- 受控类别不能再通过旧的 `PUT /affairs/{id}/process` 直接办结；普通不受控类别完全沿用原有流转。

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。
