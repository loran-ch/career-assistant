# B／E 组测试记录

> 对应分工表「每个人要为自己的模块提交使用说明和测试记录」的要求。
> 配套使用说明：[B 组](B_用户与简历_使用说明.md) · [E 组](E_邮件分享看板_使用说明.md)

---

## 1. 测试环境

| 项 | 值 |
| --- | --- |
| 操作系统 | Windows 11（24H2） |
| Python | 3.12.6（项目 `.venv`） |
| 测试框架 | pytest ≥ 8.0（见 `src/career_assistant/requirements-dev.txt`） |
| 数据库 | 临时 SQLite 库（每个用例独立，`tmp_path` 隔离） |
| 网络 | **全部离线**：用例不访问招聘网站、不调用真实 AI、不连接真实 SMTP |
| 外部依赖 | 全部用替身（假 AI 引擎、假 SMTP 服务器、假文件） |

安装测试依赖：

```powershell
python -m pip install -r src\career_assistant\requirements-dev.txt
```

---

## 2. 运行记录

```powershell
$env:CAREER_HOME = (Get-Location).Path
.\.venv\Scripts\python.exe -m pytest src\career_assistant\tests\ -q
```

最近一次完整运行结果：

```
........................................................................ [ 52%]
.................................................................        [100%]
137 passed in 36.74s
```

| 指标 | 值 |
| --- | --- |
| 用例总数 | **137** |
| 通过 | **137** |
| 失败 / 跳过 | 0 / 0 |
| 耗时 | 36.74 s |

> 未产生任何快照文件或缓存文件；测试使用临时目录，结束后自动清理。

---

## 3. 用例分布

| 测试文件 | 对应分工表模块 | 用例数 | 归属 |
| --- | --- | --- | --- |
| `tests/test_permissions.py` | 模块 3 匿名会话与权限 | 21 | B |
| `tests/test_upload.py` | 模块 4 PDF 上传与简历存储 | 17 | B |
| `tests/test_pages.py` | 模块 5 用户页面 | 16 | B |
| `tests/test_resume_versions.py` | 模块 6 简历纠正与版本 | 17 | B |
| `tests/test_polling.py` | 模块 5 页面轮询与「重复刷新」回归 | 15 | B |
| `tests/test_email.py` | 交付表 E：邮件 | 20 | E |
| `tests/test_share.py` | 交付表 E：分享 | 11 | E |
| `tests/test_api.py` | 交付表 E：看板／申请 JSON 接口 | 8 | E |
| `tests/test_application_pack.py` | 交付表 E：模拟填表（投递信息包） | 12 | E |
| `tests/conftest.py` | 公共夹具（`store` / `app` / `client` / `csrf` / `auth_client` / `resume` / `seed_job`） | — | 共用 |
| **合计** | | **137** | |

---

## 4. B 组测试明细（86 例）

### 4.1 模块 3 · 匿名会话与权限（`test_permissions.py`，21 例）

| 用例 | 验证点 |
| --- | --- |
| `test_anonymous_page_is_redirected_to_login` | 未登录访问 `/jobs`、`/profile`、`/applications`、`/resumes/1`、`/tasks/1` 一律 302 跳登录 |
| `test_anonymous_api_returns_401_json` | 未登录访问 `/api/v1/*` 返回 401 JSON，不是 302 |
| `test_anonymous_upload_and_write_are_blocked` | 未登录不能上传、不能写 |
| `test_first_run_without_account_redirects_to_register` | 库里没有账户时跳 `/register` |
| `test_session_with_unknown_user_is_treated_as_anonymous` | `user_id` 指向不存在的账户 → 按未登录处理，且会话里的 `user_id` 被清除 |
| `test_session_is_bound_to_the_logged_in_account` | 登录后的会话绑定到正确账户 |
| `test_logout_invalidates_the_session` | 登出后会话失效 |
| `test_login_next_parameter_cannot_redirect_offsite` | `next` 参数不能跳到站外 |
| `test_post_without_csrf_is_rejected` | 缺 CSRF → 403 |
| `test_post_with_wrong_csrf_is_rejected` | CSRF 错误 → 403 |
| `test_delete_is_covered_by_csrf` | `DELETE` 也受 CSRF 保护 |
| `test_json_body_can_carry_the_csrf_token` | JSON body 里的 `csrf` 可用 |
| `test_non_local_host_is_refused` | 非本机 Host → 403 |
| `test_public_share_is_refused_from_unknown_host` | 未放行主机访问公开分享 → 403 |
| `test_allowed_host_can_read_public_share` | `CAREER_SHARE_ALLOWED_HOSTS` 放行后可读 |
| `test_public_share_never_returns_resume_text` | 公开摘要不含简历原文 |
| `test_only_one_account_can_be_created` | 第二个账户无法注册（单账户模型） |
| `test_register_page_redirects_once_account_exists` | 已有账户后 `/register` 直接跳走 |
| `test_register_creates_the_first_account` | 注册页可创建首个账户 |
| `test_task_list_never_returns_payload` | 任务列表接口不含任务载荷 |
| `test_security_headers_are_set` | 安全响应头存在 |

### 4.2 模块 4 · PDF 上传与简历存储（`test_upload.py`，17 例）

| 用例 | 验证点 |
| --- | --- |
| `test_extract_pdf_reads_text` | 文本型 PDF 能提取文字 |
| `test_extract_pdf_rejects_empty_file` | 空文件被拒 |
| `test_extract_pdf_rejects_renamed_non_pdf` | 改名的非 PDF 被拒 |
| `test_extract_pdf_rejects_scanned_image_only_document` | 扫描件（无文字层）被拒 |
| `test_extract_pdf_rejects_encrypted_document` | 加密 PDF 被拒 |
| `test_extract_pdf_rejects_broken_document` | 损坏 PDF 被拒 |
| `test_extract_pdf_rejects_oversized_document` | 超限文件被拒 |
| `test_upload_requires_login` | 未登录不能上传 |
| `test_missing_file_is_rejected` | 没选文件被拒 |
| `test_empty_file_is_rejected` | 空文件被拒 |
| `test_non_pdf_content_is_rejected` | 非 PDF 内容被拒 |
| `test_oversized_upload_is_rejected` | 超过 10 MB 返回 413 |
| `test_upload_stores_a_random_private_filename` | 落盘文件名随机、权限私有 |
| `test_same_name_upload_keeps_both_records` | 同名上传保留两条记录 |
| `test_repeated_request_creates_separate_records_and_tasks` | 重复请求各自成记录与任务 |
| `test_task_idempotency_key_is_reused` | 分析任务幂等键复用 |
| `test_stored_pdf_is_only_reachable_after_login` | 已保存的 PDF 登录后才能取到 |

### 4.3 模块 5 · 用户页面（`test_pages.py`，16 例）

| 用例 | 验证点 |
| --- | --- |
| `test_upload_page_is_chinese_and_has_a_pdf_input` | `lang="zh-Hans"`、`type="file" name="resume" accept="application/pdf"`、10 MB 提示 |
| `test_upload_page_lists_saved_resumes` | 上传页列出已保存简历并可进入详情 |
| `test_task_status_page_shows_the_real_state` | 页面显示库里的真实 `failed` 状态与「AI 超时」原因 |
| `test_task_status_page_shows_uncertain_state` | 显示「结果不确定」与「可能重复发送」 |
| `test_task_status_page_reports_missing_facts` | 无事实时显示「尚无可用事实」，确认后变「已保存，可用于匹配」 |
| `test_task_page_shows_email_attempts` | 邮件每一次尝试的四态都列出 |
| `test_task_page_of_unknown_task_is_an_error` | 不存在的任务 → 400 + 「记录不存在」 |
| `test_report_page_shows_audit_items_with_evidence` | 审计条目逐条展示并带原文依据 |
| `test_report_page_says_when_no_report_exists` | 无报告时明确提示「AI审计尚未生成」 |
| `test_resume_page_shows_the_original_text_and_download_link` | 原始提取文本与 PDF 下载入口 |
| `test_application_board_page_groups_by_status` | 申请页按状态分列并给总览计数 |
| `test_application_board_page_filters_by_keyword` | 看板保留全貌，筛选显示「当前显示 1 条（全部 2 条）」 |
| `test_application_filtered_list_only_contains_matches` | 筛选列表只含匹配项 |
| `test_unknown_application_status_is_rejected` | 非法状态 → 400 + 「未知申请状态」 |
| `test_pages_declare_a_responsive_viewport` | 主页面都声明 viewport |
| `test_stylesheet_keeps_narrow_screen_breakpoints` | 样式表保留 ≤560px 断点与看板／信息包样式 |

### 4.4 模块 6 · 简历纠正与版本（`test_resume_versions.py`，17 例）

| 用例 | 验证点 |
| --- | --- |
| `test_correction_creates_a_new_version_and_keeps_the_old_one` | 纠正产生新版本，旧版本仍在 |
| `test_confirmed_text_cannot_be_overwritten` | 已确认的原文不被覆盖 |
| `test_facts_require_parsed_resume_text` | 原文未解析时拒绝确认事实 |
| `test_report_must_belong_to_the_same_resume` | 报告与简历不一致时报错 |
| `test_route_keeps_the_previous_version` | 走真实路由时旧版本保留 |
| `test_route_keeps_evidence_and_origin_of_untouched_facts` | 未改动的行继承依据与来源标记 |
| `test_route_marks_new_lines_as_user_supplied` | 新增行标为 `user_edited` |
| `test_route_marks_the_very_first_version_as_user_provided` | 第一版全部标为 `user_provided` |
| `test_identical_facts_do_not_create_a_new_version` | 内容相同不建新版本 |
| `test_empty_facts_are_rejected` | 空事实被拒 |
| `test_correction_requires_login` | 未登录不能纠正 |
| `test_reanalysis_keeps_the_previous_report` | 重新分析不覆盖旧报告 |
| `test_new_version_points_to_the_latest_report` | 新版本指向最新报告 |
| `test_same_task_does_not_produce_two_reports` | 同一任务不产生两份报告 |
| `test_reanalyze_task_reuses_the_saved_report` | 已有报告时不再调用 AI |
| `test_resume_page_lists_every_version` | 页面列出全部事实版本 |
| `test_resume_page_shows_all_reports` | 页面列出全部报告 |

### 4.5 模块 5 补充 · 页面轮询与「重复刷新」（`test_polling.py`，15 例）

对应上一轮反馈「很多搜索会一直重复刷新」，以及本轮「邮件卡片一直刷新」。这组用例把四条边界钉住：
轮询只有一份实现、整页刷新只在状态跃迁时发生一次、重复提交搜索不堆积任务、
比较前剔除每次都变的 `operation` 隐藏域。

| 用例 | 验证点 |
| --- | --- |
| `test_pages_use_the_shared_polling_script` | 所有页面引同一份 `poll.js`，模板里不再各写一份内联轮询 |
| `test_polling_script_is_served` | 轮询脚本可被浏览器取到（200） |
| `test_polling_script_only_reloads_on_a_state_change` | 刷新条件是 `status !== from`，不无条件刷新 |
| `test_polling_script_compares_normalized_html` | 比较前先归一化，避免 `hidden=""` 差异导致每轮都判为「有变化」 |
| `test_polling_script_keeps_away_from_inputs_in_progress` | 用户正在输入时本轮不刷新页面 |
| `test_polling_script_ignores_the_volatile_operation_field` | 比较前剔除 `operation` 隐藏域：它每次请求都换新值，带着它比较则每 1.5 秒都被判定为「有变化」 |
| `test_task_status_fragment_is_stable_once_the_operation_field_is_removed` | 预览态任务的片段两次拉取只差 `operation`，剔除后必须逐字相同（“一直刷新”的真实复现） |
| `test_jobs_page_records_the_state_it_was_rendered_with` | 岗位页带上渲染当时的任务状态（`data-reload-from`） |
| `test_jobs_page_marks_a_finished_task_as_already_loaded` | 任务已结束时起点状态就是终态，不再触发整页刷新 |
| `test_task_page_no_longer_reloads_the_whole_page` | 任务详情页不再每三秒整页刷新，改为统一轮询 |
| `test_task_page_shows_whether_facts_are_available` | 任务页能显示简历事实是否已保存 |
| `test_repeated_search_reuses_the_running_task` | 重复点「开始搜索」复用正在跑的任务，任务数不增 |
| `test_search_creates_a_new_task_after_the_previous_one_finished` | 上一个搜索结束后再点会真正新建任务 |
| `test_search_route_also_reuses_the_running_task` | 「搜索岗位」按钮这个入口同样不堆积任务 |
| `test_jobs_page_explains_the_scheduled_search_when_enabled` | 岗位页说明重复点击不会堆积搜索 |

---

## 5. E 组测试明细（51 例）

### 5.1 邮件（`test_email.py`，20 例）

| 用例 | 验证点 |
| --- | --- |
| `test_missing_smtp_host_only_previews` | 没有 `SMTP_HOST` → `preview`，不发送 |
| `test_missing_recipient_is_a_definite_failure` | 缺收件人 → `failed` |
| `test_missing_password_is_a_definite_failure` | 有账号没密码 → `failed` |
| `test_remote_plain_connection_is_refused` | 远端明文连接被拒（要求 SSL/STARTTLS） |
| `test_accepted_when_server_returns_positive_reply` | 服务器正向应答 → `accepted` |
| `test_connection_failure_before_send_is_failed` | 发送前连接失败 → `failed` |
| `test_disconnect_during_send_is_uncertain` | `DATA` 阶段断连 → `uncertain` |
| `test_refused_recipient_is_reported_without_resending` | 收件人被拒时报告拒收数，不整封重发 |
| `test_email_task_records_each_state` | 四态逐条写进 `course_email_attempts` |
| `test_accepted_attempt_blocks_automatic_retry` | 已有 `accepted` 记录时禁止重试 |
| `test_authentication_failure_reports_the_server_reason` | 认证失败时带出服务器真实错误码（535）与回复，不再只显示异常类名 |
| `test_smtp_error_never_leaks_the_authorization_code` | 错误信息里绝不出现授权码 |
| `test_refused_recipient_error_names_the_address` | 收件人被拒时错误信息列出地址与拒收代码 |

### 5.2 分享（`test_share.py`，11 例）

| 用例 | 验证点 |
| --- | --- |
| `test_token_is_never_stored_in_plaintext` | 库里只有 sha256 摘要与前 8 位提示，没有明文令牌 |
| `test_summary_contains_no_personal_text` | 摘要不含姓名、联系方式、简历原文等个人文本 |
| `test_invalid_token_is_rejected` | 无效令牌被拒 |
| `test_revoked_token_is_rejected` | 已撤销令牌被拒 |
| `test_expired_token_is_rejected` | 过期令牌被拒 |
| `test_access_count_is_tracked` | 访问计数被记录 |
| `test_share_page_shows_the_link_and_the_qr_code` | **页面入口**：简历详情页点「生成分享链接」直接渲染分享结果页，页面上有链接、二维码（base64 内嵌）与「只显示这一次」提示 |
| `test_share_page_never_shows_personal_text` | 分享结果页不含姓名、邮箱、简历原文等个人文本 |
| `test_share_token_is_not_exposed_in_the_url_or_session` | 明文令牌不进 URL、不进 session |
| `test_share_page_can_revoke_the_link` | 分享结果页点「撤销这条分享链接」→ 302 跳走且令牌立即失效 |
| `test_revoked_link_returns_404_to_the_visitor` | 撤销后对方再打开公开端点 → 404 |

### 5.3 看板与申请 JSON 接口（`test_api.py`，8 例）

| 用例 | 验证点 |
| --- | --- |
| `test_api_requires_login` | 未登录访问接口返回 401 |
| `test_board_groups_applications_by_status` | 看板按状态分列 |
| `test_applications_filter_by_status_and_keyword` | 状态 + 关键词筛选生效 |
| `test_share_round_trip` | 创建 → 读取 → 撤销的完整链路 |
| `test_mutating_api_requires_csrf` | 写接口需要 CSRF |
| `test_share_of_unknown_report_is_not_found` | 报告不存在 → 404 |
| `test_unknown_share_token_is_not_found` | 令牌不存在 → 404 |
| `test_share_token_is_stored_hashed` | 令牌以哈希存储 |

### 5.4 模拟填表 / 投递信息包（`test_application_pack.py`，12 例）

| 用例 | 验证点 |
| --- | --- |
| `test_pack_uses_saved_delivery_information` | 使用已确认的投递信息版本 |
| `test_pack_marks_missing_fields_without_inventing_them` | 缺失字段标「需本人填写」，**不编造** |
| `test_pack_carries_job_and_resume_facts` | 带上岗位信息与简历事实 |
| `test_pack_records_the_sources_it_used` | 记录每一项的依据来源 |
| `test_pack_versions_are_immutable` | 历史版本不被覆盖 |
| `test_pack_markdown_marks_what_is_still_missing` | Markdown 导出单列「仍需本人补齐」 |
| `test_pack_of_unknown_application_is_rejected` | 申请不存在 → 404 |
| `test_pack_module_never_touches_the_network` | **信息包模块全程不碰网络**（边界守卫） |
| `test_pack_route_requires_login` | 未登录不能取信息包 |
| `test_pack_route_returns_preview_and_saved_version` | 预览不落库、保存产生新版本 |
| `test_pack_export_returns_markdown_attachment` | 导出为 Markdown 附件 |
| `test_pack_page_shows_the_saved_version` | 申请页展示已保存的版本 |

---

## 6. 手工验收记录

| 场景 | 步骤 | 结果 |
| --- | --- | --- |
| 未配置 SMTP | 报告页点发送邮件 | 任务页显示「未发送，仅预览」，可展开预览正文，未产生外发邮件 |
| 已配置 SMTP（163） | 设置 `SMTP_*` 后重试 | 状态变为「服务器已接受，未验证送达」；再点重试被拒，提示「SMTP 已有服务器接受记录，不能重复重试。」 |
| 分享链接 | 简历详情页生成链接 → 无痕窗口打开 | 只显示计数型摘要；撤销后链接立即失效 |
| 分享结果页 | 简历详情页点「生成分享链接」 | 页面直接给出链接 + 复制按钮 + 二维码 + 脱敏摘要 + 撤销按钮；撤销后对方打开返回 404 |
| 申请看板 | 批量改状态后打开 `/applications` | 按 7 个状态分列，总览计数正确；关键词筛选只影响下方列表 |
| 投递信息包 | 申请页生成信息包 → 导出 Markdown | 字段标「已就绪 / 需本人填写」，缺失项汇总成节；导出文件为可复制粘贴的清单 |
| SMTP 授权码 | 126 邮箱授权码已写入 `.env`（本机）与 `.env.docker`（容器），两者均未被 Git 跟踪 | 报告页点发送 → **服务器已接受，未验证送达**；另实发过一封连通性测试邮件到 QQ 收件箱 |
| 本轮真实外发说明 | 调试「预览态不发」时在真实库上跑过一次推送 | 触发了一封**真实的**审计报告邮件（收件人为 `.env` 的 `SMTP_TO`）；验证用的任务记录已删除，但邮件已发出，需知悉 |
| 页面轮询 | 打开 `/jobs` 并提交一次搜索 | 进度片段实时更新、页面不闪烁不入场动画反复重播；任务完成后只整页刷新一次 |
| 重复点搜索 | 连点几次「开始搜索」 | 只产生**一个**任务，任务列表不堆积，页面不再反复刷新 |
| 邮件卡片反复刷新 | 打开带邮件任务的简历页（`?task=…`） | 卡片首屏就在，不再每 1.5 秒重写；页面不闪烁、不反复重载 |
| 预览态邮件卡片 | 未配 SMTP 时点「推送审计」 | 标题显示「邮件 · 未发送，仅预览」而非「完成」；下方给出原因与「按当前配置重新发送」按钮 |
| 配好 SMTP 后重发 | 填好 `.env` 并重启 → 点「按当前配置重新发送」 | 新建一条任务真发出去，卡片变为「邮件 · 已发送，服务器已接受」；再点「推送审计」提示「不会重复发送」 |

> SMTP 真实发信依赖本人账号的客户端授权码与网络条件；
> 自动化用例一律使用替身 SMTP 服务器，**不消耗真实邮箱**。

---

## 7. 已知限制

| 限制 | 说明 |
| --- | --- |
| 单用户模型 | 按产品定位维持单账户，不做五用户隔离；相关边界以「未登录 / 越权 / 会话识别」三条可测规则覆盖 |
| 真实 SMTP 未纳入自动化 | 自动化用替身服务器；真实发信结果见第 6 节手工记录 |
| 真实 AI 未纳入自动化 | AI 相关用例使用替身引擎（`FakeEngine`），保证离线可重复 |
| 扫描件与加密 PDF | 按「解析失败」处理并保留原件，不做 OCR 兜底 |
| 局域网分享 | 需要显式配置 `CAREER_SHARE_ALLOWED_HOSTS`，默认只允许本机 |
| 未做浏览器端到端测试 | 页面级断言基于 Flask 测试客户端，未引入 Selenium/Playwright；轮询行为靠对 `poll.js` 源码与模板属性的断言守住 |
| 定时搜索仍会自动再搜 | `--schedule` 开启时每 `poll_interval_minutes`（默认 20 分钟）自动搜一次且启动即跑首轮。本次只改 B／E，未动调度器；已在岗位页给出提示与关闭方式 |
| 真实发信只验证到「服务器已接受」 | 用 126 邮箱（`smtp.126.com`，465 + SSL）真实发过一封测试邮件，SMTP 返回 `accepted`；「已送达对方收件箱」无法由程序确认，需人工核对 QQ 收件箱 |
| 邮件任务状态与投递结果分开 | 任务 `status=succeeded` 只代表 Worker 把这段代码跑完了；邮件到底发没发出去，以 `result.status`（preview / failed / accepted / uncertain）为准，卡片文案按后者显示 |
