# EvoTask v0.4.1：多智能体 Web 工作台

现已提供完整的浏览器工作台与 FastAPI 后端：任务创建、实时协作轨迹、人工审批、产物预览与下载、知识库、群聊与私聊、经验查看，以及系统状态。原有 CLI、多 Agent 协商与再委派、文本 RAG、Docker、失败归档和自动评测继续可用。CLI 保持 Python 标准库依赖；Web 服务使用可选依赖。

新增个人多知识库：创建命名库、编辑资料及来源 URL、按任务选择检索范围。Agent 现在获得上下游验收要求和来源交接，Verifier 发现上游缺陷可退回作者，保留原测试进行最多两轮复验；历史失败任务可在页面点击「修复并复验」。详见 [协作流程与个人知识库](docs/collaboration.md)。

## Web 快速开始

在项目目录执行以下命令，然后打开 **http://127.0.0.1:8000**：

```powershell
.\.venv\Scripts\python.exe -m evotask serve
```

本次已建立 `.venv` 并安装 Web 依赖。新机器可以运行 `powershell -File .\start-web.ps1`，或手动安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[web]"
.\.venv\Scripts\python.exe -m evotask serve
```

页面默认「离线演示」，无需 API 额度即可体验 **发起任务 → 协作检索 → 预览报告 → 人工确认 → 下载产物**。顶部可切换到真实模型；真实任务需要有效 API 额度。demo/live 各自独立。顶部「登录同步」支持注册与登录，同账号的知识库、任务和对话可在不同浏览器/设备访问；访客仍按浏览器隔离。Web 数据保存于 `.evotask-web/`，不会自动导入 CLI 的历史记录。访客知识库可在账号面板导入；跨设备需访问同一服务器地址，详见 [账号与同步](docs/accounts.md)。

知识库和任务附件已取消扩展名白名单、单文件大小、附件数量限制；支持批量上传、进度提示和原件下载。常见文档会自动提取正文，大文件在磁盘上分块索引，Agent 可分段读取。扫描件/图片尚未接入 OCR，加密或暂不支持的二进制格式会标记为「仅保存原件」。实际容量仍取决于服务器磁盘、解析器内存和部署代理设置。

前端无需 Node.js、构建步骤、CDN 或外部字体。[Web 操作、接口与部署说明](docs/webapp.md) 包含局域网访问、访问口令、测试命令及当前边界。

**当前可以直接运行离线演示。真实任务需要课程允许的模型 API；执行生成代码还需要可用的 Docker Linux 容器运行时。程序不会用宿主机执行来替代 Docker，也不会在 API 失败时悄悄切到模拟模型。**

## 1. 快速运行

在当前项目目录执行：

```powershell
python -m evotask demo
```

这个明确标注为 DEMO 的流程会：

1. 导入本地 `.md` 教学资料。
2. 由协调逻辑登记研究和报告任务，按能力回复分派。
3. Researcher 再委派 Verifier 检索证据，接收经过授权复制的产物。
4. Coder 根据交接资料生成有来源标记的 `report.md`。
5. 自动核验声明的条件，保存运行报告、产物快照、事件和各 Agent 的经验。

DEMO 的模型决策是确定性 fixture，实际执行文件、检索、交接、归档和检查。它**不调用 LLM、不执行生成代码，不代表真实模型完成率或学习效果**。

默认数据存放在 `.evotask-demo/`；每次运行都有独立 ID。命令会打印报告路径及指标。查看运行和事件：

```powershell
python -m evotask --state-dir .evotask-demo list
python -m evotask --state-dir .evotask-demo status <run_id>
python -m evotask --state-dir .evotask-demo events <run_id>
```

`<run_id>` 等尖括号内容是占位符，执行时替换为命令实际输出，不包含尖括号。

## 2. 人工验收与进程恢复

```powershell
python -m evotask demo --review
```

任务会停在 `waiting_human`，输出问题、请求 ID、待验收产物及 SHA256。可以退出当前进程，查看归档后再运行：

```powershell
python -m evotask --state-dir .evotask-demo resume <run_id> <request_id> yes
```

审批回复必须为 `yes` 或 `no`；普通澄清请求则用自然语言回答。旧请求 ID 不能重复使用；审批期间文件变化会使旧批准失效。真实模式恢复会从运行记录读取原配置，密钥仍从环境变量获取。

对于没有待回复请求、但因终端退出中断的运行：

```powershell
python -m evotask continue <run_id>
```

如果上次中断发生在工具执行中，系统将其标记为“结果不确定”，保留证据并终止相应任务；不会自动重放可能已经生效的推送、删除或代码操作。

## 3. 接入课程模型

程序现在默认读取当前目录 `.env`。已有配置可直接使用；首次配置时，从模板创建并在本地编辑：

```powershell
Copy-Item .env.example .env
```

如果 `.env` 已存在，不要用上述命令覆盖。主要配置项：

| 设置 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | 本地填写密钥，不能提交版本控制 |
| `EVOTASK_PROVIDER` | `deepseek` 或课程允许的 `hkgai` |
| `EVOTASK_BASE_URL` | 模型接口地址 |
| `EVOTASK_MODEL` | 课程指定／账户可用的模型标识 |
| `EVOTASK_API_KEY_ENV` | 密钥变量的名称，不是密钥值 |
| `EVOTASK_API_TIMEOUT` | 单次模型请求超时秒数；模板设为 120 |
| `EVOTASK_THINKING_MODE` | `default/enabled/disabled`；仅对 DeepSeek 显式发送 |
| `EVOTASK_MAX_RESPONSE_TOKENS` | 单次响应 token 上限；模板设为 4096 |
| `EVOTASK_MAX_MODEL_CALLS` | 每次任务的调用预算 |
| `EVOTASK_DOCKER_IMAGE` | 代码沙箱镜像 |

其他 `Config` 字段也可用 `EVOTASK_` 加字段大写进行配置。保留对原有 `API_KEY / BASE_URL / MODEL` 的兼容支持。配置优先级为 **显式 `--config` JSON ＞ 进程环境变量 ＞ `.env` ＞ 默认值**；同一来源内优先采用 `EVOTASK_*` 名称。密钥从环境读取，不写入运行配置快照。

因此，如果旧 `config.local.json` 里仍写着占位模型，显式传入它会覆盖 `.env`。只使用 `.env` 时，无须 `--config`：

```powershell
python -m evotask doctor
python -m evotask doctor --live
python -m evotask run "创建 greeting.md，包含 EvoTask ready，并检查文件内容。"
```

`doctor` 默认不联网调用模型；`doctor --live` 最多发送一次小规模连通性请求，检查 JSON 回复。二者都不打印密钥。可以用 `--env-file path/to/file.env` 指定其他文件，或 `--no-env` 禁用 dotenv 加载。dotenv 不执行 shell 命令、不展开变量、不注入 `PATH` 等无关环境设置；配置文件支持单行引号和注释。

`.env`、`.env.*` 和 `*.env` 已被 Git 忽略，只有不含真实凭据的 `.env.example` 可提交。保留 `BRAVE_API_KEY`、`EVOTASK_GITHUB_REMOTE` 和 `EVOTASK_GITHUB_TOKEN` 为空不会影响基础任务；只有启用对应能力时才需要配置。

DeepSeek HTTP 402 表示账户余额不足；出现该错误时程序会报告原因并终止运行，避免为每个子任务继续发送无效请求。需在官方账户恢复额度或改用课程允许且有额度的密钥，再运行 `doctor --live`。HTTP 402 不代表已经验证模型存在或完整请求格式正确。[DeepSeek 错误码](https://api-docs.deepseek.com/quick_start/error_codes/)

模板对结构化任务选择 `EVOTASK_THINKING_MODE=disabled`，以控制联调延迟；没有更换你选定的模型。DeepSeek 官方说明思考模式默认开启，可用 `thinking.type` 切换；需要思考模式时再显式调整并测试响应 token 预算。[DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)

真实模式使用 HTTPS `/chat/completions` 和 JSON 对象输出协议。模型返回经过控制层校验的 `tool / ask / delegate / finish / fail` 动作，然后由程序真正执行工具。这是应用层结构化工具协议，**不是直接依赖厂商原生 function-calling 循环**。

DeepSeek 模式限制目标为 `api.deepseek.com`，没有其他厂商回退。HKGAI 模式可以配置课程提供的 `base_url`、`model`、`api_key_env`，但当前适配器假设其支持相同的 Chat Completions 与 JSON 输出协议。**未拿到课程 HKGAI 文档或完成实测前，不能保证该接口兼容；若不兼容，需要调整 `providers.py`。**

默认 API 请求不自动重试，避免在无法确定响应结果时重复消耗额度；运行预算统计失败请求。网络错误会留下失败记录。

## 4. 文本资料、RAG 和私聊

```powershell
python -m evotask kb add examples/knowledge/similarity.md
python -m evotask kb search "cosine empty input" --agent researcher
python -m evotask run "根据知识库说明 cosine similarity 与空输入策略，输出有引用的 report.md。"
```

Web 知识库接受任意格式上传，无固定大小上限；PDF、DOCX、XLSX、PPTX、OpenDocument、RTF、HTML 以及可识别编码的文本会提取正文后索引，其他文件保留原件并提示未解析。CLI 的 `kb add` 接受已规范化的 `.md/.txt`。使用 BM25，英文按词项处理，中文使用单字和相邻双字；不调用嵌入服务。来源标记包含文档、版本与片段 ID。再次导入同一路径会更新其索引；`kb remove <document_id>` 删除索引。

`--scope shared` 是显式共享资料；也可 `--scope researcher` 仅允许该 Agent 直接检索。RAG 知识和 Agent 私有经验分开保存。检索范围在评分之前过滤；受控交接可以分享已明确输出的资料摘要。该单用户原型不提供语义信息流证明，不能保证模型不会把自己可读的敏感内容写入输出；不要把敏感资料加入用于群组任务的知识库。

任务输入可直接授权给本次任务的全部 Agent：

```powershell
python -m evotask run "整理输入文本，输出 summary.md。" --input examples/input.txt
```

群聊与私聊：

```powershell
python -m evotask chat --agent group
python -m evotask chat --agent researcher --message "解释你的职责和工具范围。"
python -m evotask history --agent researcher
```

`chat` 是独立对话，用 `/quit` 退出；`run` 用来发起实际工具任务。群组中会记录用户目标及各 Agent 的任务反馈，普通群聊回复由 Coordinator 提供。私聊记录不会自动加入群组任务上下文，也不会让另一 Agent 读取。CLI 操作者作为本地人类管理员可以查看所有记录。

## 5. Docker 代码执行

在本机启用 Docker 的 Linux 容器模式，并显式准备镜像：

```powershell
docker pull python:3.11-slim
python -m evotask doctor
```

执行示例：

```powershell
python -m evotask run "编写 solution.py，实现 count_words(text)。由另一 Agent 编写并在 Docker 中执行空字符串和多空白测试，最后输出 result.md。"
```

每次代码执行创建一个短期容器，只挂载当前 Agent、本次运行的工作区；授权输入额外挂载为只读。容器禁用网络，根文件系统只读，限制 CPU、内存、进程数、时间和输出量。密钥、宿主机根目录、Docker socket 和其他 Agent 工作区均不挂载。

容器镜像未就绪时明确失败，不自动拉取，也不退回本地 Python。Linux 上建议以普通用户运行控制器；拒绝以宿主 root 身份执行生成代码。默认镜像仅含 Python 标准库；需要额外库时由开发者维护经过检查的镜像，并配置 `docker_image`，Agent 不能自行联网安装。

Docker 执行环境需要在目标机器做真实隔离验证；单元测试中的命令检查不等于已经证明容器隔离。此原型没有实现宿主磁盘配额、网络代理级 DNS 绑定或对内核逃逸的防护，不能作为不可信公众代码的生产托管服务。

## 6. GitHub 提交与推送

在配置中设置 `github_remote` 为专门的 HTTPS GitHub 测试仓库，例如 `https://github.com/your-org/your-test-repo.git`，并在终端配置 `EVOTASK_GITHUB_TOKEN`。不要把凭据放进 URL。

Coder 的 `git_publish` 工具会先生成不可变文件快照、分支和哈希预览，并暂停等待批准。只有得到对应请求的 `yes` 后，才在控制层管理的独立仓库中 commit 和 push，然后用远端 SHA 校验。

- 只能发布到 `evotask/<name>` 分支，不支持主分支覆盖或 force push。
- 已存在且内容不同的分支会被拒绝。
- Agent 不可写控制层仓库的 Git 配置与 hooks；不会在生成代码可修改的 `.git` 中运行宿主 Git。
- 密钥通过子进程环境传入 Git，不写入提交、远端地址或事件日志。
- 本次开发未向任何 GitHub 仓库推送，也未伪造团队贡献历史。平台提交能力与成员开发贡献是两件不同的事。

## 7. 失败归档与产物导出

每次失败保存错误类型、观察到的事实、已有文本／代码产物的版本和哈希，并向群组反馈；未经证实的根因不自动当作事实。运行结束后生成 `status.md`，失败时另有 `failure.md`。

取回某个版本的产物：

```powershell
python -m evotask export <run_id> <artifact_id> outputs/report.md
```

导出会核验哈希，且拒绝覆盖已有目的文件。失败的草稿也可导出，但会显示其验证状态。`verified` 仅表示声明的验收检查通过，不表示文本每个事实或代码所有行为都正确。

## 8. 测试与评测

```powershell
python -m unittest discover -s tests -v
python -m evotask evaluate --demo
python -m evotask evaluate --suite benchmarks/live.jsonl
```

测试使用 Python 标准库 `unittest`，无须安装 pytest。覆盖任务 DAG、协商与再委派、私聊和知识库权限、版本化审批、重启恢复、失败归档、预算、无记忆对照、Docker 不可用时拒绝宿主执行，以及真实本地 Git 提交。

Docker 和镜像就绪后，显式开启真实容器测试：

```powershell
$env:EVOTASK_DOCKER_TESTS = "1"
python -m unittest tests.test_docker_integration -v
```

这两项测试实际执行代码，检查只读输入、工作区隔离、禁用网络和无限循环的超时终止；默认跳过，不将未运行的隔离检查计作通过。

评测自动生成 `performance.md` 和内部 `results.json`，报告总体任务完成率、子任务指标、耗时与失败记录。外部用例检查独立于 Agent 自己声明的检查；编程用例还会在单独的 Docker 工作区运行基准断言。失败、异常和等待人工输入的任务不会从分母中删除。

默认每个评测用例使用新的状态目录，不在用例间学习。单独运行任务默认会积累经验；可用 `run --no-memory` 关闭经验读写。首版的经验是经过执行证据核验的成功／失败记录与提醒，不自动训练模型或修改权限；当前还没有证明真实模型因这些经验而提升，需用获准 API 做消融实验。

## 9. 首版范围与后续工作

实现状态和环境限制见 [docs/implementation.md](docs/implementation.md)，架构见 [docs/architecture.md](docs/architecture.md)。

首版采用预置角色、稳定身份和动态委派，调度是串行的显式状态机；没有引入 LangGraph，以减少依赖并使状态可检查。尚未实现任务中角色热切换、跨 Agent 执行权迁移、多任务并行、Electron 安装包或多用户远端服务。它们属于后续增量，不影响先验证课程核心流程。

官方 API 参考：[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)。模型标识和额度以课程配置为准，不在项目中固定为某个最新型号。
