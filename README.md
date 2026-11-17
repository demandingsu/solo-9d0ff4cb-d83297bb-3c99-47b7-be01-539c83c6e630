# 市政采购公告档案

面向市政采购档案员的公告留存工具：输入公告号，从本地示范站采集招标公告正文与唯一附件，按站点修订号逐版留档。网站改稿、换附件、撤稿后，旧版仍可核对、下载。

技术栈：FastAPI + SQLite；Docker Compose 同时启动档案应用与可在浏览器改稿的本地示范站。

## 快速开始（Docker Compose）

```bash
docker compose up --build -d
```

- 示范站（可改稿/换附件/撤稿）：<http://localhost:8000>
- 档案采集应用：<http://localhost:8001>
- 健康检查：`curl -s http://localhost:8001/healthz`

首次启动示范站会写入两条示例公告（`ZFCG-2026-0001`、`ZFCG-2026-0002`）。在档案首页输入公告号即可采集。

停止并保留数据：`docker compose down`；同时删除数据卷：`docker compose down -v`。

## 本地运行（不使用 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 终端 1：示范站
DEMO_DB_PATH=./data/demo.db python -m uvicorn demo.main:create_app --factory --port 8000

# 终端 2：档案应用
ARCHIVE_DB_PATH=./data/archive.db \
SOURCE_BASE_URL=http://127.0.0.1:8000 \
python -m uvicorn app.main:create_app --factory --port 8001
```

## 使用流程

1. 打开档案首页，输入示范站上的公告号，点击“采集公告”。
2. 在示范站打开同一公告：
   - **改稿**：修改标题/正文并保存，站点修订号 +1；再次采集会新增存档版本。
   - **替换附件**：上传新附件，站点修订号 +1；再次采集会留存新附件，旧附件仍可下载。
   - **撤稿 / 恢复**：撤稿后站点对访客隐藏内容；再次采集只更新撤稿状态，不新建版本，历史版本仍可下载。
3. 档案公告页可查看版本历史、逐版正文与附件下载、采集/核对记录。

## 一致性规则

采集流程先读元数据、下载附件、再读一次元数据，并要求：

- 两次读到的修订号、正文、附件描述、撤稿状态完全一致（防止采集中途站点改稿）；
- 正文修订号与附件修订号相同；否则整次拒收；
- 附件实际字节长度与 SHA-256 与源站声明一致；否则整次拒收。

与档案比对时：

| 情况 | 行为 |
| --- | --- |
| 修订号更新 | 新增一个存档版本（正文 + 附件同事务写入） |
| 修订号相同、内容一致 | 不重复建版（`unchanged`） |
| 修订号相同、内容不同 | 报告源站冲突（`conflict`），不覆盖旧版 |
| 修订号回退 | 报告冲突，不创建、不覆盖 |
| 同号仅撤稿状态变化 | 更新公告级撤稿标记，不新建版本 |

**不可变与原子性**：版本行（正文、附件字节与指纹）一经写入不再修改；撤稿状态保存在公告级。每个版本的公告行、版本行、附件 BLOB、日志在同一 `BEGIN IMMEDIATE … COMMIT` 中写入，SQLite 开启 WAL + `synchronous=FULL`。写入失败会回滚；进程在提交前被杀死，重启后只会看到完整旧版或没有新版，不会出现半份版本。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试覆盖：

- 首次采集、同修订重复采集、改稿、换附件、撤稿/恢复；
- 正文/附件修订不一致整次拒收；
- 同号不同内容冲突且旧版不变；
- 源站修订号回退；
- 附件 SHA-256/长度不符拒收；
- 数据库写入失败回滚、无半份版本；
- 子进程在事务提交前硬退出（模拟进程中断）后重启无残留；
- 档案 Web 页面、下载、错误提示与健康检查。

## 目录

```
app/                档案应用（domain / store / source / capture / web）
demo/               本地示范站（可编辑页面 + JSON API）
tests/              pytest 测试
Dockerfile          demo 与 archive 共用镜像
docker-compose.yml  两个服务与持久化卷
```

## 源站 API（示范站）

- `GET /api/notices/{公告号}`：公告元数据（含修订号、唯一附件描述）
- `GET /api/notices/{公告号}/attachment?rev=N`：附件字节

档案应用通过 `SOURCE_BASE_URL` 指向该站。若接入其他站点，只需实现相同的元数据字段与附件校验语义。
