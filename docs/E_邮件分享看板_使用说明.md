# E 组交付说明 · 邮件、报告分享、申请看板与投递信息包

> 对应交付表 E 行：**邮件、分享、看板、申请、模拟填表**。
> A／B／C／D 负责的模块本次未做任何改动。
> 本文是 E 组的「使用说明」，配套的「测试记录」见 [B/E 测试记录](BE_测试记录.md)。

---

## 1. 交付内容一览

| 交付项 | 实现 | 主要位置 |
| --- | --- | --- |
| 邮件 | SMTP 单次尝试 + 四态记录（preview / failed / accepted / uncertain）、每日汇总、报告与匹配结果邮件、失败重试 | `src/career_assistant/integrations.py`（`send_email`）、`src/career_assistant/digest.py`、`app.py`（`/email/...`、`/settings/digest`） |
| 分享 | 一次性令牌分享报告链接、sha256 摘要落库、脱敏摘要、**分享结果页（链接 + 复制 + 二维码 + 撤销）**、撤销与访问计数 | `src/career_assistant/share.py`、`app.py`（`/reports/<id>/share`、`/shares/revoke`） |
| 看板 | 申请状态分列看板 + 总览计数 | `templates/workspace.html`（`page="applications"`）、`store.query_application_board`、`store.board_summary` |
| 申请 | 状态流转、事件留痕、依据关联、状态／关键词／日期筛选与分页 | `app.py`（`/applications`）、`store.transition_application`、`store.link_application_basis`、`store.query_applications` |
| 模拟填表 | **本地投递信息包**：把已保存的依据整理成可自行填写的清单 | `src/career_assistant/application_pack.py`、`app.py`（`/applications/<id>/pack`） |

新增文件：

- `src/career_assistant/share.py`（171 行）——分享令牌
- `src/career_assistant/api.py`（296 行）——JSON 接口蓝图
- `src/career_assistant/application_pack.py`（233 行）——投递信息包

数据库迁移：`SCHEMA_VERSION` 从 13 升到 **14**，新增 `course_share_tokens` 与 `course_application_packs` 两张表。
迁移是幂等的，升级前会自动在同目录生成 `.bak` 备份。

---

## 2. 邮件

### 2.1 SMTP 配置

凭据**只从环境变量读取**，不写进代码、不写进仓库。

#### 2.1.1 当前已配置的位置

授权码已经写好并可直接生效，放在两个**被 Git 忽略**的文件里：

| 文件 | 生效场景 | 读取方式 | 是否进 Git |
| --- | --- | --- | --- |
| `.env`（项目根目录） | 本机直接跑 `python main.py web` | 程序启动时自动加载 | ❌ 已被 `.gitignore` 忽略 |
| `.env.docker` | `docker compose up` | `docker-compose.yml` 的 `env_file` 读取 | ❌ 已被 `.gitignore` 忽略 |

当前配置（126 邮箱发件 → QQ 邮箱收件）：

| 变量 | 值 |
| --- | --- |
| `SMTP_HOST` | `smtp.126.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USER` | `yaohongda_2006@126.com` |
| `SMTP_PASSWORD` | 126 **客户端授权码**（已写入 `.env` / `.env.docker`） |
| `SMTP_FROM` | 同发件账号 |
| `SMTP_TO` | `492083474@qq.com` |
| `SMTP_SSL` | `true` |
| `SMTP_STARTTLS` | `false` |

> 126 与 163 同属网易，SMTP 入口分别是 `smtp.126.com` 与 `smtp.163.com`，端口均为 465 + SSL。
> **换邮箱时要连 `SMTP_HOST` 一起换**，只改账号不改服务器会直接连错机器。

> **为什么授权码不写在 `.env.docker.example` 里**：该文件**会被 Git 跟踪**（`.gitignore` 里对它有 `!` 放行），
> 而本仓库的远程地址是公开的 GitHub 仓库。把真实授权码写进去等于公开泄露账号。
> 因此 `.env.docker.example` 只保留占位符，真实凭据只放在未跟踪的 `.env.docker` 里。
> 改配置时改 `.env` / `.env.docker`，**不要**改 `.env.docker.example`。

> **优先级**：进程里已存在的环境变量 > `.env` 文件。已经在终端里 `export` 过同名变量时，`.env` 不会覆盖它。

#### 2.1.2 其他配置方式

推荐做法是复制 `.env.example` 为本机 `.env`（已被 `.gitignore` 忽略），或直接在启动前 export：

```bash
# Linux / macOS
export SMTP_HOST=smtp.126.com
export SMTP_PORT=465
export SMTP_USER=<你的126邮箱账号>
export SMTP_PASSWORD=<126客户端授权码，不是登录密码>
export SMTP_FROM=<发件人邮箱>
export SMTP_TO=<收件人邮箱，可逗号分隔多个>
export SMTP_SSL=true
export SMTP_STARTTLS=false
```

```powershell
# Windows PowerShell
$env:SMTP_HOST = "smtp.126.com"
$env:SMTP_PORT = "465"
$env:SMTP_USER = "<你的126邮箱账号>"
$env:SMTP_PASSWORD = "<126客户端授权码>"
$env:SMTP_FROM = "<发件人邮箱>"
$env:SMTP_TO = "<收件人邮箱>"
$env:SMTP_SSL = "true"
$env:SMTP_STARTTLS = "false"
```

> **安全提醒**：126 邮箱的 `SMTP_PASSWORD` 是「客户端授权码」，不是网页登录密码。
> 该授权码等价于账号密码，**不要**写进 `.env.example`、`.env.docker.example`、`README`
> 或任何提交到 Git 的文件，只放在本机 `.env` / `.env.docker` 或启动终端的环境变量里。

配置项说明：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `SMTP_HOST` | 是 | 留空 → 进入 `preview` 状态，只生成预览不发送 |
| `SMTP_PORT` | 否 | 默认 `465`（SSL）或 `587`（STARTTLS） |
| `SMTP_USER` / `SMTP_PASSWORD` | 是 | 账号与授权码；`SMTP_PASSWORD` 不为空但 `SMTP_USER` 为空同样视为配置不完整 |
| `SMTP_FROM` | 是 | 缺省取 `SMTP_USER` |
| `SMTP_TO` | 是 | 支持逗号分隔多个收件人 |
| `SMTP_SSL` | 否 | 默认 `true`，`1/true/yes` 均视为开启 |
| `SMTP_STARTTLS` | 否 | 默认 `true`（当 `SMTP_SSL=false`） |

安全兜底：**非本机** SMTP（主机不是 `localhost`/`127.0.0.1`/`::1`）如果 SSL 与 STARTTLS 都关闭，
直接判为 `failed`（「非本机 SMTP 连接需要 SSL 或 STARTTLS」），不会明文发送凭据。

### 2.2 邮件四态

`integrations.send_email()` 每次尝试返回**且只返回**下面四种状态之一，每一次尝试都会写进 `course_email_attempts`：

| 状态 | 含义 | 卡片标题文案 | 是否可自动重试 |
| --- | --- | --- | --- |
| `preview` | 没有配置 `SMTP_HOST`，只生成本地预览，**没有发送** | 邮件 · 未发送，仅预览 | 可以（用户显式点重试） |
| `failed` | 连接或配置失败、SMTP 明确拒绝、收件人被拒 | 邮件 · 发送失败 | 可以 |
| `accepted` | SMTP 服务器已接受（**不代表已送达**） | 邮件 · 已发送，服务器已接受 | **不可以**，见下 |
| `uncertain` | 在 `DATA` 阶段断开，无法判断服务器是否已收到 | 邮件 · 结果不确定，请先核对 | 只能人工确认后重试 |

> **任务状态 ≠ 投递结果。** 任务 `status=succeeded` 只代表 Worker 把这段代码跑完了，
> 不代表邮件发出去了；卡片标题按 `result.status` 显示，所以预览态不会出现
> 「邮件 · 完成」这种自相矛盾的说法。

两个关键保护：

1. **`accepted` 之后禁止重试**：`store.retry` 检测到同一任务已有 `accepted` 记录时直接抛错
   「SMTP 已有服务器接受记录，不能重复重试。」，避免重复投递。
2. **`uncertain` 必须人工确认**：任务状态页显示「请先核对外部结果；重试可能重复发送」，
   重试表单带 `confirmed=yes` 隐藏字段，用户勾选确认后才会真正重试。

### 2.3 触发方式

| 入口 | 路径 | 说明 |
| --- | --- | --- |
| 报告邮件 | `POST /email/report/<report_id>` | 发送某份 AI 审计报告 |
| 匹配结果邮件 | `POST /email/match/<match_id>` | 发送某个岗位的匹配结果 |
| 每日汇总 | `POST /settings/digest` | 在「设置」页开启后由调度器每天触发 |
| 汇总预览 | `GET /settings/digest/<task_id>/preview` | 查看汇总正文，**不代表已发送** |
| 手动重试 | `POST /tasks/<task_id>/retry` | 失败 / 不确定 / 预览态才允许 |

邮件任务使用 `max_attempts=2`，并且标记 `side_effect=True`
（副作用任务的重试策略由 `store.retry` 显式控制，不会被 Worker 自动重放）。

**幂等键不是一个固定值。** 原本写死成 `email:{kind}:{record_id}`，会把页面钉死在**第一次**那条任务上：
第一次没配 SMTP 时留下的是「仅预览」，之后即使配好了 SMTP 再点也只会回到旧任务，
用户看到的永远是「未发送，仅预览」，像是根本没走邮箱配置。

现在按「这份内容**最近一次**邮件任务」的状态决定复用还是新建：

| 最近一条同内容邮件任务 | 再点「推送审计／推送推荐」的结果 |
| --- | --- |
| 还在排队／执行中 | **复用**这一条，不堆积任务 |
| 服务器已接受（`accepted`） | **不重复发送**，跳回原任务并提示「这份内容此前已发送成功（服务器已接受），不会重复发送。」 |
| 预览态（`preview`） | **新建**一条任务真发出去（幂等键带上上一条任务 id，保证每次点击都是新的一条、且不会撞键） |
| 失败态（`failed`） | **新建**一条任务重试 |

手动重试（`POST /tasks/<task_id>/retry`）同样可用：预览态的按钮文案是
**「按当前配置重新发送」**（不是「重试失败步骤」——邮件并没有失败，只是当时没配 SMTP）。
填好 `.env` 并**重启程序**后点它，就会真正发出去。

> 预览态的卡片会把原因直接写出来：「未配置 SMTP；仅生成本地预览，未发送。
> 请在 `.env` 里填好 `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_TO`，
> 重启程序后再点「按当前配置重新发送」即可真正发出。」

### 2.4 失败原因怎么读

程序会把 SMTP 服务器返回的**错误码与原始回复**一起显示在任务页上，
不再只给一个异常类名。对照下表定位：

| 页面上的错误 | 含义 | 处理 |
| --- | --- | --- |
| `SMTP 明确拒绝（…，代码 535）…… Error: authentication failed` | **凭据被拒** | 见下方「认证被拒」的四个检查点 |
| `SMTP 明确拒绝（…，代码 550）` | 收件人被服务器拒收 | 核对 `SMTP_TO` 的地址；错误信息里会列出被拒的地址 |
| `SMTP 明确拒绝（…，代码 554）` | 服务器拒绝该次投递 | 可能是发信频率过高或账号受限，稍后再试 |
| `SMTP 连接或配置失败（…）` | 根本没连上 | 检查网络、`SMTP_HOST`／`SMTP_PORT`，以及 `SMTP_SSL`／`SMTP_STARTTLS` 是否与端口匹配 |
| `SMTP 发送结果不确定（…）` | 在 `DATA` 阶段断线 | 人工核对收件箱后再决定是否重试，**不要**直接重发 |
| `未发送，仅预览` | 没有 `SMTP_HOST` | 只生成了正文，压根没配 SMTP |

**认证被拒（535）的四个检查点**，按这个顺序查：

1. **`SMTP_PASSWORD` 填的是不是客户端授权码？** 126 邮箱后台「设置 → POP3/SMTP/IMAP」里生成的
   16 位授权码，**不是**网页登录密码。填错密码就会得到 535。
2. **该邮箱的 SMTP 服务开了吗？** 126 邮箱默认不开，需要在上述页面手动开启，开启后才会生成授权码。
3. **授权码是不是旧的？** 在设置页重新生成一次，旧授权码立刻失效。
4. **`SMTP_USER` 和 `SMTP_HOST` 配对了吗？** 126 必须用 `smtp.126.com`，163 用 `smtp.163.com`，
   **不能混用**；`SMTP_USER` 也要写完整邮箱地址，不能只写用户名。

> 排查时可以在命令行直接验证（**不会真的发信**，只握手与认证）：
>
> ```powershell
> .\.venv\Scripts\python.exe -c "import smtplib,os; from career_assistant import config; c=smtplib.SMTP_SSL(os.environ['SMTP_HOST'],465); c.login(os.environ['SMTP_USER'],os.environ['SMTP_PASSWORD']); print('OK')"
> ```
>
> 报 `535 Error: authentication failed` 就是凭据问题；报连接超时才是网络问题。

---

## 3. 报告分享

### 3.1 安全模型

| 约束 | 实现 |
| --- | --- |
| 令牌明文只出现一次 | 创建接口返回一次，**分享结果页**当场显示链接与二维码，离开本页后无法再取回（库里只有摘要） |
| 数据库不存明文 | 只存 `sha256(token)` 摘要与前 8 位 `token_hint` |
| 不存完整链接 | 只存站点根地址 `base_url`；完整链接在读取时用调用方带来的明文令牌临时拼出 |
| 摘要脱敏 | 只输出**计数与分类**：事实总数、技能数、经历数、审计项数与通过率；**不含**姓名、联系方式、简历原文、AI 结论文本 |
| 摘要冻结 | 摘要在创建时冻结，报告后续变化不会改写已发出的链接内容 |
| 有效期 | 默认 7 天，可设 1–30 天；过期后按「分享链接已过期」处理 |
| 可撤销 | 分享结果页点「撤销这条分享链接」（`POST /shares/revoke`），或 `DELETE /api/v1/share/<token>`；列表接口标注 `revoked_at` |
| 访问计数 | 每次读取 `access_count + 1`，并记录 `last_accessed_at` |

### 3.2 页面入口与点击步骤

**入口在「我的资料 → 简历文件 → 点开某个简历文件」这一页**（路由 `GET /resumes/<resume_id>`），
每张「AI审计 #N」卡片底部都有「生成分享链接」按钮。

点下去之后会**直接跳到一个分享结果页**（不再是弹一行提示就没了），页面上有五块内容：

| 区块 | 内容 |
| --- | --- |
| 分享链接 | 完整链接 + 「复制链接」按钮（剪贴板不可用时自动降级为选中文本）+ 有效期与访问计数说明 |
| 二维码 | 二维码图片（`data:image/png;base64,...` 直接内嵌，不需要额外请求）；未装 `qrcode` 时显示安装提示 |
| 对方能看到什么 | 事实总数、技能数、经历／项目数、审计项数与通过率、覆盖率 —— **只有计数** |
| 不想给了？ | 「撤销这条分享链接」按钮，撤销后对方再打开是 404 |
| 回导航 | 「返回简历详情」「打开申请看板」 |

明文令牌只出现在这一个页面里：不写进 URL、不进 session、不进日志。
离开本页后**无法再找回明文**（库里只存 `sha256` 摘要），所以看到就立刻复制保存。

手机扫码打不开时（`local_only`），页面会直接提示改配置：

```bash
export CAREER_SHARE_BASE_URL=http://<本机局域网IP>:5000
export CAREER_SHARE_ALLOWED_HOSTS=<本机局域网IP>
```

改完**重启程序**并**重新生成分享链接**（旧链接里的域名不会自动变）。

### 3.3 接口方式（等价路径）

1. `POST /reports/<report_id>/share` 或 `POST /api/v1/reports/<id>/share` 创建分享，返回令牌明文，**只此一次**；
2. `GET /api/v1/share/<token>` 对方凭令牌换脱敏摘要（公开端点）；
3. `POST /shares/revoke` 或 `DELETE /api/v1/share/<token>` 撤销。

二维码：安装了 `qrcode`（已加入 `requirements.txt`）时，`GET /api/v1/share/<token>/qr.png` 返回 PNG；
未安装时接口返回 `503` 与提示「二维码组件未安装，请先安装 qrcode」，**不会**让整个流程崩掉。
页面在二维码不可用时降级为纯文本链接。

### 3.4 公开端的访问控制

公开端点只有两个：`api.public_share` 与 `api.share_qr`。

- 默认只允许 `localhost` / `127.0.0.1` 访问；
- 需要在局域网或手机上打开时，显式设置放行名单：

```bash
export CAREER_SHARE_ALLOWED_HOSTS=192.168.1.20,phone.local
export CAREER_SHARE_BASE_URL=http://192.168.1.20:5000
```

放行名单之外的来源访问公开端点仍然返回 403。

---

## 4. 申请看板

### 4.1 状态定义

| 状态 | 标签 | 状态 | 标签 |
| --- | --- | --- | --- |
| `saved` | 收藏 | `interview` | 面试 |
| `preparing` | 准备中 | `offer` | Offer |
| `applied` | 已投递 | `rejected` | 拒绝 |
| | | `withdrawn` | 撤回 |

### 4.2 页面结构

`GET /applications` 页面分三块：

1. **总览**：申请总数、各状态计数、岗位总数与今日新增、简历／报告／材料／收藏／针对性简历／分享／信息包数量；
2. **看板**：按上面 7 个状态分列，**始终是全量视图**，方便一眼看全流程；
3. **筛选**：支持 `?status=` 与 `?q=`（职位或公司关键词），只作用于下方列表；
   筛选结果会显示「当前显示 N 条（全部 M 条）」并提供「清除筛选」。

非法筛选参数（`?status=not-a-status`）返回 400 与「未知申请状态」，不会静默当成「全部」。

### 4.3 状态流转与依据关联

- `POST /applications/<id>` 提交新状态、备注与事件日期 → `store.transition_application` 记录事件；
- `POST /applications/<id>/basis` 关联「简历事实版本 + 材料」，并区分用途 `planned`（计划投递）与 `actual`（实际投递）；
  关联的材料必须与申请的岗位一致，否则报「材料与申请岗位不一致」。

---

## 5. 模拟填表 = 本地投递信息包

### 5.1 边界（最重要的一条）

> **程序不访问、不识别、不填写任何真实招聘网站的表单字段；不上传文件、不点击提交。**
> 投递动作始终由用户自己完成。

信息包只是把「已经保存的依据」整理成一份清单，并明确标出**哪一格还空着**。
这条边界与 `REQUIREMENTS.md` 第 2 章完全一致，单元测试里有专门的断言守住它。

### 5.2 信息包内容

| 分区 | 来源 | 说明 |
| --- | --- | --- |
| 基础信息 | `course_delivery_profiles` 最新版本、申请记录、岗位来源记录 | 姓名、邮箱、电话、地区、个人链接、应聘职位、公司、原岗位链接、工作地点、信息来源、当前申请状态 |
| 事实分组 | `course_resume_versions` 最新版本 | 教育背景／工作实习经历／项目经历／技能／其他事实，每条都带原文依据 |
| 结构化资料 | `course_profile_versions` 最新版本 | 技能、经历、证书、补充材料 |
| JD 问题草稿 | `course_materials` | 来自材料里的问题草稿，标注「需本人确认」 |
| 附件清单 | `course_resumes` | 只列出本机已保存的 PDF，注明「请自行上传到招聘网站」 |
| 求职信草稿 | `course_materials` | 草稿原文 |

每个字段都带 `status`：

- `ready` → 「已就绪」，有内容；
- `unknown` → 「需本人填写」，**绝不用编造内容填空**。

`missing` 列出所有仍需本人补齐的字段，Markdown 导出时单列一节。

### 5.3 版本与导出

| 操作 | 路径 | 说明 |
| --- | --- | --- |
| 预览（不落库） | `GET /api/v1/applications/<id>/pack` | 按当前依据组装一份，不写库 |
| 生成新版本 | `POST /api/v1/applications/<id>/pack` | 保存为**新版本**，历史版本不被覆盖 |
| 页面生成 | `POST /applications/<id>/pack` | 同上，生成后 flash 提示「已生成投递信息包版本 #N；请自行前往招聘网站填写与提交」 |
| 导出 Markdown | `GET /applications/<id>/pack/export` | 下载 `application-pack-<id>.md` |

---

## 6. JSON 接口

所有 `/api/v1` 接口（除两个公开端点外）都需要登录态；写操作还需要 CSRF
（`X-CSRF` 头或 JSON body 的 `csrf` 键）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/board` | 看板：按状态分列的卡片 + 汇总计数，**不含个人材料** |
| `GET` | `/api/v1/applications` | 申请筛选：`status`、`q`、`from`、`to`、`page`、`page_size` |
| `GET` | `/api/v1/tasks` | 任务列表：`kind`、`status`、分页；**不返回载荷** |
| `GET` | `/api/v1/jobs` | 岗位列表：`q`、`source`、分页 |
| `POST` | `/api/v1/reports/<id>/share` | 创建分享，返回令牌明文（只此一次） |
| `GET` | `/api/v1/shares` | 分享列表：只给 `token_hint` 与站点根地址 |
| `DELETE` | `/api/v1/share/<token>` | 撤销分享 |
| `GET` | `/api/v1/share/<token>` | **公开**：凭令牌换脱敏摘要 |
| `GET` | `/api/v1/share/<token>/qr.png` | **公开**：分享链接二维码 |
| `GET` | `/api/v1/applications/<id>/pack` | 投递信息包预览 |
| `POST` | `/api/v1/applications/<id>/pack` | 生成并保存信息包新版本 |

---

## 7. 演示步骤（5 分钟）

1. `python main.py web --port 5000`，注册并登录；
2. **邮件**：不配置 `SMTP_HOST` → 在报告页点「发送邮件」→ 任务页显示「未发送，仅预览」并可展开预览正文；
   配好 `SMTP_*` 后重试 → 状态变为「服务器已接受，未验证送达」；
3. **分享**：简历详情页点「生成分享链接」→ 进入**分享结果页**（链接 + 复制按钮 + 二维码 + 脱敏摘要 + 撤销按钮）
   → 复制链接另开无痕窗口打开 → 只看到计数型摘要；点页面上的「撤销这条分享链接」→ 对方再打开变 404；
4. **看板**：把几个岗位分别标成「收藏 / 准备中 / 已投递 / 面试」→ 打开 `/applications` 看分列与总览；
   在筛选框输入关键词 → 列表只剩匹配项，看板仍是全量；
5. **投递信息包**：打开某条申请 → 点「生成投递信息包」→ 页面上每个字段标着「已就绪 / 需本人填写」，
   缺失项汇总在「仍需本人补齐」；点「导出 Markdown」拿到可复制粘贴的清单。

---

## 8. 边界声明

- 不访问真实招聘网站表单、不上传附件、不代为提交；
- 分享链接只输出计数型摘要，不输出姓名、联系方式与简历原文；
- 令牌明文不落库、不进日志、不回显；
- 邮件「服务器已接受」不等于「已送达」，程序不宣称送达；
- 未经人工确认，`uncertain` 的邮件不会被自动重发。
