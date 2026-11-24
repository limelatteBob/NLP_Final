# EvoTask · 多智能体 Web 工作台

EvoTask 是一个 NLP 课程项目，由 Coordinator、Researcher、Coder 和 Verifier 协作拆解任务、检索资料、生成产物并验证结果。当前版本 **v0.4.1**，采用 Python / FastAPI、SQLite 和原生 JavaScript / CSS，前端无需 Node.js 或构建步骤。

## 核心功能

- **Agent 协作**：任务拆解、能力协商、子任务委派、独立验证与最多两轮返修。
- **个人知识库**：多库管理、资料上传与编辑、来源标注、任务选库、整库删除及默认库清空。
- **账号同步**：在同一服务内跨浏览器、跨设备使用知识库、任务和对话。
- **过程可追溯**：查看协作事件、人工审批、失败原因、产物版本及 Agent 经验，预览并下载结果。

新增自定义团队：在「Agent 对话」填写四位成员的名称和职责，在群内发起任务、回答问题、选择上一轮交付继续开发。支持 HTML 下载、完整团队记录及评测包 ZIP 导出；见 [操作与中期测试](docs/mid-test.md)。

## 新用户上手（Windows）

Clone 或下载仓库后，在**项目根目录打开 PowerShell**。以下步骤用于运行真实模型任务，首次安装需要联网。

1. **准备环境**：安装 Python 3.11+，勾选 **Add Python to PATH**；安装并启动 [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)，按提示配置 WSL 2，使用 Linux 容器模式。Docker 用于执行 Agent 生成的代码和 Verifier 测试，报告类任务也可能用到。

2. **配置模型**：首次复制模板并编辑；已有 `.env` 时直接编辑，避免覆盖。

也可运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\start-web.ps1`，由脚本创建环境、安装依赖并启动。其他平台的命令见 [Web 手册](docs/webapp.md#启动与体验)。

   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```

   模板默认使用 DeepSeek，至少填写以下两项并保存；模型账户需有可用额度，密钥勿提交到 GitHub。

   ```dotenv
   DEEPSEEK_API_KEY=你的有效API密钥
   EVOTASK_MODEL=课程允许且账户可用的模型标识
   ```

3. **准备执行镜像**：确认 Docker Desktop 已运行，逐条执行，成功后继续。

   ```powershell
   docker version
   docker pull python:3.11-slim
   ```

4. **启动项目**：脚本自动创建虚拟环境、安装依赖并启动服务。

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\start-web.ps1
   ```

   执行策略放行仅作用于本次 PowerShell 进程。保持此终端和 Docker Desktop 运行；若组织策略限制脚本，使用 [Web 手册](docs/webapp.md#启动与体验) 中的手动启动命令。

5. **发起任务**：打开 **http://127.0.0.1:8000**，可注册登录，切换到「真实模型」，填写目标并启动；按页面提示确认交付后下载产物。例如：

   > 实现一个 Python 词频统计函数，由独立 Agent 编写测试并在 Docker 中执行，交付代码和使用说明。

**下次启动**：打开 Docker Desktop，再执行第 4 步即可，无需重复配置或拉取镜像。修改 `.env` 后需重启服务。

**只体验离线演示**：安装 Python 后直接执行第 4 步，保持页面的「离线演示」模式；无需 API 密钥或 Docker。演示使用固定教学任务，自定义目标需切换真实模式。

macOS/Linux 启动方法见 [Web 手册](docs/webapp.md#启动与体验)。模型参数、HKGAI 适配边界与连通性检查见 [配置手册](docs/cli.md#模型与环境配置)；运行 `.\.venv\Scripts\python.exe -m evotask doctor` 可检查配置和 Docker，不调用模型。

## 账号、数据与部署

顶部「登录同步」支持注册账号和导入访客知识库。**跨设备同步需要访问同一台服务**；各自启动的本地实例拥有独立账号和数据，演示与真实模式也各自独立。

Web 数据保存在 `.evotask-web/`；备份与恢复见 [账号手册](docs/accounts.md#存储与认证)。

团队页支持群聊及 Agent 私聊。真实模型统一用中文回复和撰写交付正文；每次只问一个关键问题，可点选简短答案或自行填写。删除和发布操作仍需明确确认。

Push 到 GitHub 后，新用户可按上述步骤本地运行。若希望用户直接打开网址使用，需要部署 Python 后端、持久化数据目录并配置访问控制与 HTTPS；代码执行所需的 Docker 由服务器提供，访问网站的用户无需本地安装。部署方法见 [局域网访问](docs/accounts.md#让其他设备连接) 和 [Web 部署](docs/webapp.md#对外体验与部署)，当前使用单个 Uvicorn worker。

## 开发与验证

在另一个终端安装测试依赖并运行回归：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[web,test-web]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

常规测试不调用付费模型；真实 Docker 用例需显式启用。浏览器验证见 [Web 测试说明](docs/webapp.md#验证)，离线评测、真实模型评测及经验消融见 [评测手册](docs/cli.md#测试与评测)。

## 文档导航

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

当前 Agent 工作流使用四个固定内部位置和串行调度，可自定义成员名称与职责，暂不支持任务中角色热切换或 Electron 安装包。上传不设固定格式或大小限制，但实际容量与可解析格式有边界；尚未支持 OCR，无法提取正文的文件只保存原件。
