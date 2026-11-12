# 账号与跨设备同步（v0.4）

知识库、任务记录、产物、Agent 经验和对话现在可以跟随账号保存。浏览器只保存登录 Cookie；数据集中保存在运行 EvoTask 的服务器。另一浏览器或设备访问**同一个服务地址**并登录同一账号，就使用同一份资料。分别在两台电脑运行本地服务不会自动互联。

## 使用流程

1. 打开 Web 工作台，点击顶部「登录同步」→「注册账号」。设置用户名、显示名称（可选）和至少 12 字符的密码；用户名不区分大小写，支持字母、数字、点、下划线和短横线。
2. 注册后进入账号面板。已有访客资料会显示预览，点击「导入到我的账号」复制当前模式的知识库。演示和真实模式分别导入；没有资料可以跳过。
3. 另一浏览器/设备打开相同服务地址，点击「登录同步」，使用相同用户名和密码登录。配置了服务访问口令时，要先输入管理员提供的访问口令；它与个人密码不同。
4. 知识库列表在页面可见、没有编辑/上传操作时，每 12 秒检查更新；也可点击「立即同步」。表单编辑中保留输入。文档设置保存时检查版本，其他设备已修改则返回冲突提示，不自动覆盖；先复制尚未保存的内容，再同步后编辑。
5. 点击顶部账号名称，可查看登录设备、退出其他设备、修改密码或退出当前设备。改密码会撤销其他登录；退出当前设备不影响其他设备继续使用。

同一账号的 demo/live 各自独立，任务选库和 Agent 文档作用域继续生效。同账号、同模式仍只有一个写任务；另一设备不能在执行中并发修改知识库。关闭页面或退出账号不会自动停止服务器上已经提交的任务，需要停止时先使用任务详情的「停止」。

## 访客资料导入规则

- 登录前的访客空间继续保留。导入为显式的一次性复制，不自动上传到外部云服务。
- 复制命名库、说明、文档原件和规范化文本、来源 URL、访问范围及检索分块；新增库名带「访客导入」标识。文档、库和原件分配新 ID，不覆盖账号内的同名资料。
- 当前浏览器、当前模式在此账号中只导入一次。再次点击不会新增副本；导入后修改账号副本与访客原件互不影响。
- 本次导入不包含访客任务、对话、经验或旧产物。登录前的访客任务仍保存在原目录；新账号任务可直接跨设备访问。
- 很早版本的索引若本来就没有关联原件，仅复制已有分块和引用位置，不伪造或拼接为原文件；之后可重新上传原件。
- 普通复制失败会回滚索引并清理本次创建的副本，可重新导入；不会删除访客原件。进程意外退出可能留下未入索引的上传目录，重试不会覆盖已有账号数据。
- 退出或会话过期会换一个空访客身份，避免在共享设备上直接返回旧资料。需要旧访客知识库时，请在首次登录后的账号面板中先导入。

## 让其他设备连接

本机默认地址仍是 `http://127.0.0.1:8000`。要在可信局域网体验，先确定服务器的实际 IPv4 地址（例如 `192.168.1.20`），在服务器 `.env` 中配置：

```dotenv
EVOTASK_WEB_ACCESS_TOKEN=<替换为至少24字符的随机服务访问口令>
EVOTASK_WEB_ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.20
```

这里的 IP 只是示例，必须替换。服务访问口令是独立凭证，不要填写模型 API 密钥。可用以下命令在本机生成随机口令并自行填入 `.env`：

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

停止旧服务后，在项目目录启动：

```powershell
.\start-web.cmd -BindAddress 0.0.0.0 -Port 8000
# 或
.\.venv\Scripts\python.exe -m evotask serve --host 0.0.0.0 --port 8000
```

其他设备连接同一网络，访问 `http://192.168.1.20:8000`。如 Windows 防火墙拦截，管理员需允许该私有网络上的对应入站端口。不要在其他设备输入 `127.0.0.1`，它指向那台设备自身。localhost 与局域网 IP 的 Cookie 独立；在新地址重新登录账号即可。

公网或非可信网络使用 HTTPS 反向代理。保留实际 Host，在 `.env` 中列出域名；反向代理终止 TLS 时，通过 `serve --trusted-proxy <代理IP>` 信任具体代理，以正确设置 Secure Cookie。无需重新打包前端。部署细节见 [Web 部署](webapp.md#对外体验与部署)。本次没有部署公网服务器或自动修改防火墙。

## 存储与认证

```text
.evotask-web/
├── session.key                         # 访客/服务门禁签名密钥
├── accounts.sqlite3                    # 用户、密码哈希、会话摘要和登录限速
├── accounts/<服务端生成的用户ID>/
│   ├── demo/                           # SQLite、文档、对话、任务及归档
│   └── live/
└── sessions/<访客ID>/
    ├── demo/
    └── live/
```

密码使用独立随机盐的 scrypt（N=2^17，r=8，p=1），同时限制密码派生并发数。参数依据 [OWASP 密码存储建议](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html#scrypt)，实现使用 [Python hashlib.scrypt](https://docs.python.org/3.11/library/hashlib.html#hashlib.scrypt)。账号数据库不存明文密码或完整会话令牌。

登录令牌为 256 位随机值，服务端只保存 SHA-256 摘要；有效期固定 30 天，不因普通活动无限续期。Cookie 设置 HttpOnly、SameSite=Strict，在 HTTPS 下设置 Secure。注销/改密撤销数据库会话，已撤销的 Cookie 重放不能访问账号数据；账号身份变化使旧 CSRF 失效，旧页不能把写入提交给新账号。活动 SSE 流会检查会话撤销状态。同浏览器标签页登录变化会通知其他标签页清空旧界面并重新载入。

认证操作按来源地址（20 次/分钟）和用户名（10 次/分钟）限制尝试。未知用户名也执行密码派生，错误信息不回显输入。账号 ID 从有效会话解析，业务接口不接受客户端指定任意账号空间。服务访问口令继续保护整套服务及注册入口；账号隔离不表示每个账号拥有独立的模型密钥或额度，真实模式使用服务器配置的模型账户。

备份应在停止服务后整体复制 `.evotask-web/`；运行中仅复制 SQLite 主文件可能遗漏 WAL。恢复账号数据库、用户目录和 `session.key` 后，用户可以重新登录。服务器管理员及能直接读取磁盘的本机用户不受 Web 账号权限隔离。

本版支持单服务器、单 Uvicorn worker；尚未提供邮箱找回密码、管理员重置界面、SSO/MFA、端到端加密、跨独立服务器同步或离线编辑合并。请保存账号密码和服务器备份。分布式部署仍需共享存储、任务队列和统一限速。

## 接口与验证

写请求须使用 `/api/session` 返回的 `X-EvoTask-CSRF` 和 JSON；登录、注册、改密、退出后重新读取 CSRF。`GET /api/session` 返回 `user`（访客为 null）和 `account_expired`；`authenticated` 仍表示服务访问口令是否通过。

| 接口 | 功能 |
|---|---|
| `POST /api/account/register` | username、password、可选 display_name；注册并登录 |
| `POST /api/account/login` | 用户名密码登录 |
| `POST /api/account/logout` | 撤销当前登录并切换空访客空间 |
| `GET /api/account/devices` | 本账号有效登录设备，不返回 Cookie 或令牌 |
| `POST /api/account/logout-others` | 撤销其他设备登录 |
| `POST /api/account/password` | current_password、new_password；改密并更新当前令牌 |
| `GET /api/account/guest-knowledge?mode=live` | 当前浏览器的可导入库与是否已导入 |
| `POST /api/account/import-guest?mode=live` | 复制当前浏览器当前模式的访客知识库 |
| `PATCH /api/knowledge/{id}` | 可传 expected_revision；与文档详情返回的 revision 比较，过期编辑返回 409 |

后端测试：`python -m unittest tests.test_accounts -v`。浏览器测试先用独立状态目录启动测试服务，再执行：

```powershell
.\.venv\Scripts\python.exe -m evotask --no-env --state-dir .evotask-auth-test serve --port 8001
# 另一个终端
.\.venv\Scripts\python.exe tests/browser_accounts.py --url http://127.0.0.1:8001
```

浏览器回归用两个独立 Cookie 上下文模拟桌面与手机，验证注册、访客导入、跨设备读写与自动刷新、原件下载、并发编辑冲突、会话撤销、改密与退出；没有调用付费模型。它验证不同浏览器身份的同步行为，不代替实际手机网络、HTTPS 证书或防火墙验收。
