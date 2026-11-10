# B 组交付说明 · 用户、简历上传、页面与简历版本

> 对应分工表**模块 3、4、5、6**。A／C／D 负责的模块本次未做任何改动。
> 本文是 B 组的「使用说明」，配套的「测试记录」见 [B/E 测试记录](BE_测试记录.md)。

---

## 1. 交付内容一览

| 分工表模块 | 本组实现 | 主要位置 |
| --- | --- | --- |
| 3 匿名会话与权限 | 单用户会话、登录态识别、CSRF 校验、本机主机限制、首个账户注册 | `src/career_assistant/app.py`（`protect_local` / `register` / `login` / `logout`） |
| 4 PDF 上传与简历存储 | 10 MB 上限、PDF 文件头校验、随机文件名、私有权限、幂等任务键 | `app.py`（`upload`）、`store.add_resume` |
| 5 用户页面 | 中文上传页、任务状态页、报告页、申请看板页；响应式布局；显示**真实**任务状态与错误；统一轮询、不再反复整页刷新 | `templates/page.html`、`templates/workspace.html`、`templates/task_progress.html`、`static/workspace.css`、`static/poll.js` |
| 6 简历纠正与版本 | 事实确认产生新版本、旧版本保留、重新分析不覆盖旧报告、依据版本关联 | `app.py`（`confirm`）、`store.confirm_facts`、`store.resume_waiting` |

---

## 2. 环境准备与启动

```powershell
# 1. 激活虚拟环境（Windows）
.\.venv\Scripts\Activate.ps1

# 2. 安装运行依赖与开发依赖
python -m pip install -r requirements.txt
python -m pip install -r src\career_assistant\requirements-dev.txt

# 3. 指定数据根目录并初始化数据库（可重复执行，幂等）
$env:CAREER_HOME = (Get-Location).Path
python main.py migrate

# 4. 启动本机网页
python main.py web --port 5000
```

浏览器打开 <http://127.0.0.1:5000>。首次访问会跳转 `/register` 创建**唯一**的本机账户；之后所有页面与接口都需要先登录。

> 默认 SQLite 模式下 `web` 内置单个 Worker，不要同时再起 `worker` 进程。
> 停止服务前请先退出网页服务，`migrate` 需要独占数据库写锁。

---

## 3. 模块 3：会话与权限

### 3.1 权限模型（重要说明）

本项目定位是**单用户、本机浏览器运行**的个人工具，`REQUIREMENTS.md` 明确不建设多人平台。
因此分工表里「五用户隔离」在本实现中落地为三条**可测的边界**：

| 分工表要求 | 本实现的落地方式 | 对应测试 |
| --- | --- | --- |
| 五用户隔离 | 单账户模型：全库只有一个账户，第二个账户无法注册 | `test_only_one_account_can_be_created` |
| 越权访问 | 未登录访问页面 → 302 跳转登录；未登录访问 `/api/` → 401 JSON；跨主机访问 → 403 | `test_anonymous_page_is_redirected_to_login`、`test_anonymous_api_returns_401_json`、`test_non_local_host_is_refused` |
| 会话识别 | `session["user_id"]` 指向已不存在的账户时，立即按未登录处理并清除该键 | `test_session_with_unknown_user_is_treated_as_anonymous` |

### 3.2 请求拦截规则

所有请求都经过 `app.py` 的 `before_request`（`protect_local`），顺序如下：

1. **主机限制**：只接受 `localhost` / `127.0.0.1`；公开分享端点例外，需要通过 `CAREER_SHARE_ALLOWED_HOSTS` 显式放行。
2. **CSRF**：仅对 `POST` / `PUT` / `PATCH` / `DELETE` 生效。令牌可以通过三种方式提供：
   - 表单字段 `csrf`
   - 请求头 `X-CSRF`
   - JSON body 的 `csrf` 键
   令牌存放在会话中，登录后由 `templates/_app_header.html` 注入所有表单。
3. **登录态**：未登录时，页面请求 302 跳 `/login`（首次运行跳 `/register`），`/api/` 下返回 401 JSON。
4. **输出边界**：任务列表接口只返回状态字段，**不返回任务载荷**，避免带出简历内容。

### 3.3 关键环境变量

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `CAREER_SHARE_ALLOWED_HOSTS` | 允许访问公开分享端点的主机白名单（逗号分隔） | 空（不放行） |
| `CAREER_SHARE_BASE_URL` | 生成分享链接时使用的站点根地址 | 当前请求来源 |

---

## 4. 模块 4：PDF 上传与简历存储

### 4.1 上传规则

| 规则 | 实现 | 失败表现 |
| --- | --- | --- |
| 大小上限 10 MB | 读取 `10 * 1024 * 1024 + 1` 字节判断 | 超过上限 → `413` |
| 必须是 PDF | 校验文件头 `%PDF-`，不看扩展名 | 失败 → 页面提示「文件实际内容不是 PDF」 |
| 私有存储 | 保存到 `data/course_uploads/<uuid4>.pdf`，权限 `0o600` | — |
| 随机文件名 | 用 UUID 十六进制重命名，原始文件名只作为显示字段入库 | — |
| 落库失败回滚 | `store.add_resume` 抛错时删除已落盘文件 | 不留半截文件 |
| 幂等 | 分析任务幂等键固定为 `analyze:{resume_id}` | 同一份简历不会重复排队 |

### 4.2 扫描件与加密文件

扫描件（无文字层）与加密 PDF 在解析阶段失败，**不会**被当作「无事实」静默通过：

- 任务状态显示为 `failed`，错误信息为 `PDF 已损坏或无法解析`；
- 原始 PDF 仍然保留在库中，用户可以下载或重新上传；
- 只有解析出文字后才会写入 `course_resumes.text`。

### 4.3 数据表

| 表 | 用途 | 关键字段 |
| --- | --- | --- |
| `course_resumes` | 上传的简历原件 | `filename` / `path` / `sha256` / `text` |
| `course_resume_versions` | 事实版本（不可变） | `resume_id` / `version` / `facts_json` / `origin` / `report_id` |
| `course_reports` | AI 审计报告 | `resume_id` / `data_json` / `task_id` |

---

## 5. 模块 5：用户页面

### 5.1 页面清单

| 页面 | 路径 | 说明 |
| --- | --- | --- |
| 注册 | `/register` | 仅在数据库还没有账户时可用 |
| 登录 | `/login` | 密码只以 werkzeug 安全哈希入库，不存明文 |
| 我的资料（上传页） | `/profile` | 中文上传界面，10 MB 提示，列出已保存简历与投递信息 |
| 简历详情（报告页） | `/resumes/<id>` | 原始提取文本、PDF 下载、事实版本列表、AI 审计条目与原文依据、分享按钮 |
| 事实确认 | `POST /resumes/<id>/confirm` | 一行一条事实，保存为新版本 |
| 任务详情 | `/tasks/<id>` | 任务状态、失败原因、邮件每一次尝试的四态 |
| 任务状态片段 | `/tasks/<id>/status` | 供前端轮询的轻量片段 |
| 申请看板 | `/applications` | 总览计数、状态分列看板、状态／关键词筛选 |

### 5.2 「显示真实任务状态及错误」

任务状态页渲染的是 `course_tasks` 表里的真实 `status` 与 `error` 字段，不是写死的文案：

| 库中状态 | 页面显示 |
| --- | --- |
| `queued` | 等待处理 |
| `running` | 处理中 |
| `succeeded` | 完成 |
| `failed` | 失败 + 失败原因 + 「失败原因与技术详情」折叠区 |
| `uncertain` | 结果不确定 + 「请先核对外部结果；重试可能重复发送」 |

`templates/task_progress.html` 同时用 `<span hidden data-task-status="...">` 暴露状态机读属性，
方便测试与前端脚本断言。

### 5.3 响应式布局

- 所有主页面都声明 `<meta name="viewport" content="width=device-width, ...">`；
- `static/workspace.css` 保留 `@media (max-width: 560px)` 等窄屏断点；
- 看板分列（`.board-columns`）与投递信息包字段（`.pack-fields`）在窄屏下换行，不横向溢出。

### 5.4 页面轮询与「重复刷新」的修复

上一轮反馈「很多搜索会一直重复刷新」。根因有四条，前三条属本组（模块 5 用户页面）范围，已全部修复。

| 根因 | 表现 | 修复 |
| --- | --- | --- |
| 前端每 1.5 秒**无条件**重写一次轮询容器 | 页面反复闪烁、入场动画反复重播、用户填了一半的内容被冲掉 | 新增 `static/poll.js`：先归一化再比较服务端 HTML 与浏览器 `innerHTML`，**内容没变就不动 DOM** |
| 整页刷新没有条件 | 任务状态停在终态时仍反复 `location.reload()` | 引入 `data-reload-from`（页面渲染当时的状态），刷新条件改为「状态 ∈ `data-reload-on` **且** 状态 ≠ `data-reload-from`」，天然只触发一次 |
| 重复点「开始搜索」每次新建任务 | 任务堆积，岗位页一直停在「正在获取岗位」并持续刷新 | 新增 `active_search_task()`：已有 `queued`/`running` 的搜索任务时**复用**它，不再新建 |
| 定时搜索（`--schedule`）每 20 分钟自动搜一次 | 程序自己又出现新结果，看起来像「又在刷新」 | 属调度器范围（A／D 组），本次**未改动**；改为在岗位页显式提示并给出关闭方式 |

轮询契约（所有模板统一使用，模板内不再各写一份内联轮询）：

```html
<div data-poll="/tasks/{{task.id}}/status"
     data-reload-on="succeeded,failed,uncertain"
     data-reload-from="{{task.status}}">
  {% include 'task_progress.html' %}
</div>
```

| 属性 | 含义 |
| --- | --- |
| `data-poll` | 要轮询的片段地址（`/tasks/<id>/status`） |
| `data-reload-on` | 到达这些状态时才考虑整页刷新 |
| `data-reload-from` | 页面渲染当时的状态，作为「跃迁」的起点 |

`poll.js` 另外遵守三条：

1. **不打断输入**：容器里有 `input` 事件发生、或焦点落在表单控件上时，本轮直接跳过；
2. **容错**：请求失败只是放弃本轮，下一轮继续，不会把页面刷坏；
3. **内容没变就不动 DOM**：只有真正变化时才替换 HTML，并广播 `career:motion-refresh` 让入场动画重新播放。

搜索去重规则（`app.py` 的 `active_search_task()`）：

| 场景 | 行为 |
| --- | --- |
| 已有 `queued`/`running` 的搜索任务 | 复用该任务，跳转到同一个 `/jobs?task=N` |
| 上一个搜索已结束（`succeeded`/`failed`/`uncertain`） | 真正新建一个任务 |

`/preferences`（偏好页表单）与 `/search`（岗位页「搜索岗位」按钮）两个入口都走这条规则。

---

## 6. 模块 6：简历纠正与版本

### 6.1 版本规则

| 规则 | 行为 |
| --- | --- |
| 原版本保留 | 每次确认事实都写入**新版本**（`version = MAX(version) + 1`），旧版本行不被更新或删除 |
| 新旧版本关联 | 新版本记录同一 `resume_id`；未被改动的行会继承上一版本的 `origin` 与 `evidence` |
| 新增行标记 | 相对上一版本新增的行标记为 `origin = user_edited`；第一版全部为 `user_provided` |
| 相同内容不建版本 | 事实内容与上一版本完全一致时跳过写入，避免无意义的历史膨胀 |
| 事实不能为空 | 至少保留一条事实，否则报错「事实不能为空；至少保留一条可用事实」 |
| 原文未解析 | `course_resumes.text` 为空时拒绝确认，提示「简历原文未解析，暂不能确认事实」 |
| 报告归属校验 | 关联的报告必须属于同一份简历，否则报「报告与简历不一致」 |

### 6.2 重新分析不覆盖旧报告

- `store.save_report` 带 `task_id` 时是幂等的：同一个任务只会留一份报告；
- 同一份简历可以同时存在多份报告（历史报告全部保留并在报告页列出）；
- 重新分析产生的是**新的报告记录**，旧报告不被覆盖；
- 已经存在报告的简历再次触发分析时，Worker 不会重复调用 AI（`service.analyze` 先查库）。

### 6.3 事实确认后如何继续

`POST /resumes/<id>/confirm` 保存成功后调用 `store.resume_waiting(resume_id)`：
把处于 `waiting_facts` 的准备记录改回 `queued` 并重新入队，让下游的材料生成继续使用新事实。

---

## 7. 相关接口（B 组范围）

| 方法 | 路径 | 登录 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/healthz` | 否 | 健康检查 |
| `GET` | `/profile` | 是 | 上传页与已保存简历 |
| `POST` | `/resumes` | 是（+CSRF） | 上传 PDF |
| `GET` | `/resumes/<id>` | 是 | 简历、事实版本与报告 |
| `GET` | `/resumes/<id>/pdf` | 是 | 下载原始 PDF |
| `POST` | `/resumes/<id>/confirm` | 是（+CSRF） | 确认事实，生成新版本 |
| `GET` | `/tasks/<id>` | 是 | 任务详情与邮件尝试记录 |
| `GET` | `/tasks/<id>/status` | 是 | 任务状态片段 |
| `GET` | `/applications` | 是 | 申请看板与筛选 |

---

## 8. 边界声明

- 本组**不**改造为多用户平台，也不提供公开注册、多人隔离与线上找回密码；
- 本组**不**自动填写、上传或提交任何真实招聘表单；
- 密码只以安全哈希保存在本机数据库，日志与接口都不会回显明文；
- 未配置 AI 时分析任务会失败，但**不会**影响简历上传、事实纠正、申请管理与页面浏览。
