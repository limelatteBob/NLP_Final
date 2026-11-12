# EvoTask · 多智能体 Web 工作台

EvoTask 是一个 NLP 课程项目：由 Coordinator、Researcher、Coder 和 Verifier 协作拆解任务、检索资料、生成产物并验证结果。你可以在浏览器中查看执行过程、补充信息、确认交付和下载文件。

当前版本 **v0.4.1**，采用 Python / FastAPI、SQLite 和原生 JavaScript / CSS，前端无需 Node.js 或构建步骤。

## 核心功能

- **Agent 协作**：任务拆解、能力协商、子任务委派、独立验证与最多两轮返修。
- **个人知识库**：创建多个库，上传和编辑资料、标注来源、按任务选库；支持整库删除和默认库清空。
- **账号同步**：注册登录，在同一服务内跨浏览器、跨设备使用知识库、任务和对话。
- **过程可追溯**：查看事件、人工审批、失败原因、产物版本及 Agent 经验；支持预览和导出。

## 快速开始

需要 **Python 3.11+**。从 GitHub 下载并解压仓库，或使用 Git 克隆后进入项目根目录。

以下为 Windows PowerShell 命令：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[web]"
.\.venv\Scripts\python.exe -m evotask serve
```

也可运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\start-web.ps1`，由脚本创建环境、安装依赖并启动。其他平台的命令见 [Web 手册](docs/webapp.md#启动与体验)。

打开 **http://127.0.0.1:8000**，保持终端运行。下次启动只需执行上面的最后一条命令。

首次使用可以按以下流程体验：

1. 保持「离线演示」，点击「启动协作演示」。
2. 查看任务分工、协作轨迹和生成的报告。
3. 审阅产物，点击「确认通过」，再下载结果。

离线演示无需 API 密钥或 Docker，使用固定教学任务；检索、文件交接与归档会实际执行。**自定义目标和真实模型能力需要切换到真实模式。**

## 接入真实模型

首次配置时复制模板；已有 `.env` 时直接编辑，避免覆盖：

```powershell
Copy-Item .env.example .env
```

模板默认使用 DeepSeek，至少填写以下两项：

```dotenv
DEEPSEEK_API_KEY=你的API密钥
EVOTASK_MODEL=课程允许且账户可用的模型标识
```

保存后重启服务，在页面顶部选择「真实模型」。真实任务会消耗 API 额度，密钥保留在服务端，勿提交到 GitHub。

需要运行生成代码时，另行安装并启动 Docker，使用 Linux 容器模式并准备镜像：

```powershell
docker pull python:3.11-slim
```

可运行 `.\.venv\Scripts\python.exe -m evotask doctor` 检查配置、Docker 和 Git；此检查不调用模型。连通性测试、HKGAI 适配边界和其他配置见 [配置手册](docs/cli.md#模型与环境配置)，完整参数见 [.env.example](.env.example)。

## 账号、数据与部署

顶部「登录同步」支持注册账号和导入访客知识库。**跨设备同步需要访问同一台服务**；不同电脑各自启动的本地实例拥有独立账号和数据。演示与真实模式的数据也各自独立。

Web 数据保存在 `.evotask-web/`，包括账号、知识库、对话和任务归档；备份与恢复方法见 [账号手册](docs/accounts.md#存储与认证)。

推送到 GitHub 后，新用户可以按上述步骤本地运行。如果希望用户直接打开网址使用，需要部署 Python 后端、持久化数据目录并配置访问控制与 HTTPS。部署步骤见 [局域网访问](docs/accounts.md#让其他设备连接) 和 [Web 部署](docs/webapp.md#对外体验与部署)。当前部署方式使用单个 Uvicorn worker。

## 开发与验证

安装测试依赖并运行回归：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[web,test-web]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

常规测试不调用付费模型；真实 Docker 用例需要显式启用。浏览器验证见 [Web 测试说明](docs/webapp.md#验证)，离线评测、真实模型评测及经验消融见 [评测手册](docs/cli.md#测试与评测)。

## 文档导航

| 你想了解什么 | 文档 |
|---|---|
| 页面操作、上传格式、接口与部署 | [Web 手册](docs/webapp.md) |
| 登录、跨设备同步、访客导入与备份 | [账号手册](docs/accounts.md) |
| Agent 协作、验证返修与个人知识库 | [协作流程](docs/collaboration.md) |
| CLI 命令、模型配置、Docker、GitHub 发布与评测 | [进阶使用手册](docs/cli.md) |
| 状态机、权限、RAG 与存储设计 | [架构说明](docs/architecture.md) |
| 已实现范围、测试记录与限制 | [实现记录](docs/implementation.md) |

当前 Agent 工作流采用固定角色和串行调度，暂不支持角色热切换或 Electron 安装包。文档上传不设固定格式或大小限制，但可解析格式与实际容量有边界；尚未支持 OCR，无法提取正文的文件只保存原件。
