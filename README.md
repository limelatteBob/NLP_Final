# EvoTask · 多智能体 Web 工作台 Multi-Agent Web Workbench

EvoTask 是一个 NLP 课程项目，由 Coordinator、Researcher、Coder 和 Verifier 协作拆解任务、检索资料、生成产物并验证结果。当前版本 **v0.4.1**，采用 Python / FastAPI、SQLite 和原生 JavaScript / CSS，前端无需 Node.js 或构建步骤。

EvoTask is an NLP course project in which the Coordinator, Researcher, Coder, and Verifier work together to break down tasks, retrieve information, produce artifacts, and verify results. The current version, **v0.4.1**, uses Python with FastAPI, SQLite, and vanilla JavaScript and CSS. The frontend has no Node.js dependency or build step.

## 核心功能 Core Features

- **Agent 协作**：任务拆解、能力协商、子任务委派、独立验证与最多两轮返修。

  **Agent collaboration** includes task decomposition, capability negotiation, subtask delegation, independent verification, and up to two revision rounds.

- **个人知识库**：多库管理、资料上传与编辑、来源标注、任务选库、整库删除及默认库清空。

  The **personal knowledge base** feature lets you manage multiple knowledge bases, upload and edit materials, record sources, choose knowledge bases for tasks, delete entire knowledge bases, and clear the default one.

- **账号同步**：在同一服务内跨浏览器、跨设备使用知识库、任务和对话。

  **Account sync** gives you access to your knowledge bases, tasks, and conversations across browsers and devices connected to the same service.

- **过程可追溯**：查看协作事件、人工审批、失败原因、产物版本及 Agent 经验，预览并下载结果。

  For **workflow traceability**, you can review collaboration events, human approvals, failure reasons, artifact versions, and lessons recorded by agents. You can also preview and download results.

新增自定义团队：在「Agent 对话」填写四位成员的名称和职责，在群内发起任务、回答问题、选择上一轮交付继续开发。支持 HTML 下载、完整团队记录及评测包 ZIP 导出；见 [操作与中期测试](docs/mid-test.md)。

You can now **create a custom team** in Agent Conversations (「Agent 对话」) by specifying four members' names and responsibilities. Use the group chat to start tasks, answer questions, and build on a deliverable from the previous round. You can download HTML and export the full team record and a ZIP evaluation package. See [Usage and Midterm Testing](docs/mid-test.md).

## 新用户上手 Getting Started (Windows)

Clone 或下载仓库后，在**项目根目录打开 PowerShell**。以下步骤用于运行真实模型任务，首次安装需要联网。

After cloning or downloading the repository, **open PowerShell in the project root**. These steps set up tasks that use live models. The initial installation requires an internet connection.

1. **准备环境**：安装 Python 3.11+，勾选 **Add Python to PATH**；安装并启动 [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)，按提示配置 WSL 2，使用 Linux 容器模式。Docker 用于执行 Agent 生成的代码和 Verifier 测试，报告类任务也可能用到。

   **Set up the environment** by installing Python 3.11 or later and selecting **Add Python to PATH**. Install and start [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/), follow the prompts to configure WSL 2, and use Linux container mode. Docker runs agent-generated code and Verifier tests. Report tasks may also use it.

2. **配置模型**：首次复制模板并编辑；已有 `.env` 时直接编辑，避免覆盖。

   To **configure the model**, copy and edit the template on your first setup. If `.env` already exists, edit it in place to avoid overwriting it.

也可运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\start-web.ps1`，由脚本创建环境、安装依赖并启动。其他平台的命令见 [Web 手册](docs/webapp.md#启动与体验)。

You can also run `powershell -NoProfile -ExecutionPolicy Bypass -File .\start-web.ps1` to let the script create the environment, install dependencies, and start the service. See the [Web Guide](docs/webapp.md#启动与体验) for commands on other platforms.

   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```

   模板默认使用 DeepSeek，至少填写以下两项并保存；模型账户需有可用额度，密钥勿提交到 GitHub。

   The template uses DeepSeek by default. Fill in at least the two settings below and save the file. Your model account needs available API credit. Keep the API key out of GitHub.

   ```dotenv
   DEEPSEEK_API_KEY=你的有效API密钥_your_valid_api_key
   EVOTASK_MODEL=课程允许且账户可用的模型标识_model_id_allowed_by_the_course_and_available_to_your_account
   ```

3. **准备执行镜像**：确认 Docker Desktop 已运行，逐条执行，成功后继续。

   To **prepare the execution image**, make sure Docker Desktop is running. Run the following commands one at a time and continue once both succeed.

   ```powershell
   docker version
   docker pull python:3.11-slim
   ```

4. **启动项目**：脚本自动创建虚拟环境、安装依赖并启动服务。

   **Start the project** with the script below. It creates the virtual environment, installs dependencies, and starts the service.

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\start-web.ps1
   ```

   执行策略放行仅作用于本次 PowerShell 进程。保持此终端和 Docker Desktop 运行；若组织策略限制脚本，使用 [Web 手册](docs/webapp.md#启动与体验) 中的手动启动命令。

   The execution policy bypass applies only to this PowerShell process. Keep this terminal and Docker Desktop running. If your organization restricts scripts, use the manual startup commands in the [Web Guide](docs/webapp.md#启动与体验).

5. **发起任务**：打开 **http://127.0.0.1:8000**，可注册登录，切换到「真实模型」，填写目标并启动；按页面提示确认交付后下载产物。例如：

   To **start a task**, open **<http://127.0.0.1:8000>**. You can register and sign in. Switch to Live Model mode (「真实模型」), enter your goal, and start the task. Follow the prompts to approve the delivery, then download the artifacts. You can try the example below.

   > 实现一个 Python 词频统计函数，由独立 Agent 编写测试并在 Docker 中执行，交付代码和使用说明。

   > Implement a Python function that counts word frequencies. Have an independent agent write the tests and run them in Docker. Deliver the code and usage instructions.

**下次启动**：打开 Docker Desktop，再执行第 4 步即可，无需重复配置或拉取镜像。修改 `.env` 后需重启服务。

For **subsequent launches**, start Docker Desktop and repeat step 4. The existing configuration and image are reused. Restart the service after editing `.env`.

**只体验离线演示**：安装 Python 后直接执行第 4 步，保持页面的「离线演示」模式；无需 API 密钥或 Docker。演示使用固定教学任务，自定义目标需切换真实模式。

To **try the offline demo**, install Python, go straight to step 4, and leave the page in Offline Demo mode (「离线演示」). The demo uses a preset teaching task and runs without an API key or Docker. Switch to Live Model mode to enter a custom goal.

macOS/Linux 启动方法见 [Web 手册](docs/webapp.md#启动与体验)。模型参数、HKGAI 适配边界与连通性检查见 [配置手册](docs/cli.md#模型与环境配置)；运行 `.\.venv\Scripts\python.exe -m evotask doctor` 可检查配置和 Docker，不调用模型。

## 验收与返修

新任务会将需求 ID、检查结果与实际文件哈希绑定。Python 验收必须在 Docker 内执行断言或 unittest，用例数为零不能通过；普通业务脚本不受此限制。网页交互可声明浏览器检查，实际点击、输入并核对结果，外部网络资源默认阻断。

浏览器验收需要安装 `python -m pip install -e ".[browser]"`。Windows 使用已安装的 Edge；macOS/Linux 另运行 `python -m playwright install chromium`。未安装浏览器运行环境时验收会明确失败，不回退为字符串检查。已有 `test-web` 依赖的环境包含 Playwright。

验收失败先诊断责任：业务问题返给作者，并提供冻结测试及复现输入；测试本身错误则展示具体差异，等待人工批准后复验。确认界面也可选择修改对象、填写问题并继续修订。模型诊断仍可能出错，请检查修改测试是否降低了业务要求。跨轮开发须明确选择同团队当前已验收文件；团队历史按完整记录分页读取，审批授权不会自动继承。旧任务保留原有协议。

## 账号、数据与部署
For macOS and Linux startup instructions, see the [Web Guide](docs/webapp.md#启动与体验). For model settings, HKGAI support limits, and connectivity checks, see the [Configuration Guide](docs/cli.md#模型与环境配置). Run `.\.venv\Scripts\python.exe -m evotask doctor` to check the configuration and Docker without calling a model.


顶部「登录同步」支持注册账号和导入访客知识库。**跨设备同步需要访问同一台服务**；各自启动的本地实例拥有独立账号和数据，演示与真实模式也各自独立。

Use Sign In and Sync (「登录同步」) at the top of the page to register an account and import a guest knowledge base. **Cross-device sync requires connecting to the same service instance.** Each local instance keeps its own accounts and data. Demo and live modes also keep their data separate.

Web 数据保存在 `.evotask-web/`；备份与恢复见 [账号手册](docs/accounts.md#存储与认证)。

Web data is stored in `.evotask-web/`. For backup and recovery instructions, see the [Account Guide](docs/accounts.md#存储与认证).

团队页支持群聊及 Agent 私聊。真实模型统一用中文回复和撰写交付正文；每次只问一个关键问题，可点选简短答案或自行填写。删除和发布操作仍需明确确认。

The team page supports group chats and private conversations with agents. Live models reply in Chinese and write the body of deliverables in Chinese. They ask one key question at a time, and you can select a short answer or enter your own. Deleting and publishing still require explicit confirmation.

Push 到 GitHub 后，新用户可按上述步骤本地运行。若希望用户直接打开网址使用，需要部署 Python 后端、持久化数据目录并配置访问控制与 HTTPS；代码执行所需的 Docker 由服务器提供，访问网站的用户无需本地安装。部署方法见 [局域网访问](docs/accounts.md#让其他设备连接) 和 [Web 部署](docs/webapp.md#对外体验与部署)，当前使用单个 Uvicorn worker。

Once the repository is pushed to GitHub, new users can run it locally by following the steps above. To make the app available at a URL, deploy the Python backend, persist the data directory, and configure access controls and HTTPS. The server provides Docker for code execution, so site visitors can use the app without installing Docker locally. The current setup uses a single Uvicorn worker. See [Local Network Access](docs/accounts.md#让其他设备连接) and [Web Deployment](docs/webapp.md#对外体验与部署).

## 开发与验证 Development and Verification

在另一个终端安装测试依赖并运行回归：

In another terminal, use the commands below to install the test dependencies and run the regression suite.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[web,test-web]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

常规测试不调用付费模型；真实 Docker 用例需显式启用。浏览器验证见 [Web 测试说明](docs/webapp.md#验证)，离线评测、真实模型评测及经验消融见 [评测手册](docs/cli.md#测试与评测)。

The standard tests run without paid model calls. Tests that use Docker must be explicitly enabled. For browser verification, see the [Web Testing Guide](docs/webapp.md#验证). For offline evaluation, live-model evaluation, and agent memory ablation, see the [Evaluation Guide](docs/cli.md#测试与评测).

## 文档导航 Documentation

| 你想了解什么 | 文档 |
|---|---|
| 页面操作、上传格式、接口与部署 | [Web 手册](docs/webapp.md) |
| 登录、跨设备同步、访客导入与备份 | [账号手册](docs/accounts.md) |
| Agent 协作、验证返修与个人知识库 | [协作流程](docs/collaboration.md) |
| CLI、模型配置、Docker、GitHub 发布与评测 | [进阶手册](docs/cli.md) |
| 状态机、权限、RAG 与存储设计 | [架构说明](docs/architecture.md) |
| 中文交互、课程架构要求与实测边界 | [架构核查](docs/ARCHITECTURE_CHECK.md) |
| 已实现范围、测试记录与限制 | [实现记录](docs/implementation.md) |
| 自定义团队、跨轮交付与中期实测 | [操作说明](docs/mid-test.md) · [测试报告](docs/MID_TEST_RESULTS.md) |

| What you want to learn | Documentation |
|---|---|
| Using the web interface, upload formats, APIs, and deployment | [Web Guide](docs/webapp.md) |
| Sign-in, cross-device sync, guest data import, and backups | [Account Guide](docs/accounts.md) |
| Agent collaboration, verification and revision, and personal knowledge bases | [Collaboration Workflow](docs/collaboration.md) |
| CLI, model configuration, Docker, publishing to GitHub, and evaluation | [Advanced Guide](docs/cli.md) |
| State machine, permissions, RAG, and storage design | [Architecture](docs/architecture.md) |
| Chinese-language interaction, course architecture requirements, and scope of testing | [Architecture Review](docs/ARCHITECTURE_CHECK.md) |
| Implemented features, test records, and limitations | [Implementation Notes](docs/implementation.md) |
| Custom teams, carrying deliverables across rounds, and midterm test results | [Usage Guide](docs/mid-test.md) · [Test Report](docs/MID_TEST_RESULTS.md) |

当前 Agent 工作流使用四个固定内部位置和串行调度，可自定义成员名称与职责，暂不支持任务中角色热切换或 Electron 安装包。上传不设固定格式或大小限制，但实际容量与可解析格式有边界；尚未支持 OCR，无法提取正文的文件只保存原件。

The current agent workflow uses four fixed internal slots and schedules agent work sequentially. Team member names and responsibilities are customizable. Switching roles mid-task and installing the app via an Electron package are currently unsupported. Uploads have no fixed file type or size restrictions, though storage capacity and format parsing impose practical limits. OCR is currently unavailable. If the service cannot extract the text, it keeps only the original file.
