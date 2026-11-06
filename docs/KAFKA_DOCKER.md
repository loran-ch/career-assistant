# Kafka 与 Docker Compose 运行说明

## 架构

默认本机模式仍使用 SQLite 队列，不需要 Kafka：

```text
Web -> course_tasks -> 单 Worker -> SQLite
```

课程扩展模式使用 Kafka 传递任务通知，但 SQLite 仍是任务状态、幂等、重试、
审批和审计记录的唯一事实源：

```text
Web -> course_tasks + course_task_outbox (同一 SQLite 事务)
                    |
                    v
              Outbox Relay
                    |
                    v
                  Kafka
                    |
                    v
          Worker by task_id
                    |
                    v
              SQLite final state
```

Kafka 消息只包含：

```json
{
  "schema_version": 1,
  "task_id": 123,
  "kind": "materials",
  "idempotency_key": "materials:456"
}
```

消息不包含简历正文、JD、个人资料、AI 提示词、审批内容或 SMTP 凭据。Worker
收到通知后从 SQLite 读取任务。

## 可靠性语义

- `course_tasks` 与 `course_task_outbox` 在同一个 SQLite 事务提交。
- Outbox 采用 at-least-once 发送；Kafka 发布成功后才标记为 `published`。
- Consumer 关闭自动提交，仅在任务进入 `succeeded`、`failed` 或 `uncertain`
  后提交 offset。
- 重复消息先检查任务状态和幂等键；终态任务直接确认，不重复执行。
- 可重试异常仍由现有 Worker 写回 `queued + available_at`，同一条未提交消息
  会等待可用时间后继续处理。
- 有外部副作用的任务发生不确定结果时进入 `uncertain`，不会静默自动重放。
- 为保护 SQLite、文件锁和本机浏览器档案，`worker` 必须保持单副本。

## 启动

1. 复制环境模板并按需填写 AI/SMTP 配置：

   ```sh
   cp .env.docker.example .env.docker
   ```

2. 确保 `config.yaml` 存在。Compose 会只读挂载到 `/app/config.yaml`。

3. 构建并启动：

   ```sh
   docker compose up --build
   ```

   若本机 `5000` 已被其他程序占用，可以在 `.env.docker` 或当前终端设置
   `CAREER_WEB_PORT=5050`，再访问 `http://127.0.0.1:5050`。容器内部应用端口
   始终保持 `5000`。

4. 访问 `http://127.0.0.1:${CAREER_WEB_PORT:-5000}`。可选的 Kafka UI：

   ```sh
   docker compose --profile ui up --build
   ```

   此时 Kafka UI 位于 `http://127.0.0.1:8080`。

停止服务：

```sh
docker compose down
```

该命令保留数据卷。只有明确不需要数据时才使用 `docker compose down -v`。

## 验收证据

查看健康状态与日志：

```sh
docker compose ps
docker compose logs -f web worker kafka
```

查看 topic：

```sh
docker compose exec kafka kafka-topics --bootstrap-server localhost:9092 --list
```

观察任务消息。消息体应只有任务 ID、类型和幂等键：

```sh
docker compose exec kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic career-assistant.tasks \
  --from-beginning \
  --property print.key=true
```

查看 outbox 与任务状态：

```sh
docker compose exec worker python -c "import sqlite3; c=sqlite3.connect('/app/data/radar.db'); print(c.execute('select id,task_id,status,attempts,published_at from course_task_outbox order by id').fetchall()); print(c.execute('select id,kind,status,attempts from course_tasks order by id').fetchall())"
```

恢复验证：

1. 启动一个耗时 AI 任务。
2. 执行 `docker compose restart worker`。
3. 检查外部副作用任务是否进入 `uncertain`，普通任务是否有限重试。
4. 检查同一 `task_id` 没有生成重复业务记录。

Deployment note: Kafka only replaces transport. It does not turn the application
into a multi-worker or multi-user platform.
