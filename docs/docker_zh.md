# GenericAgent Docker 打包指南（中文）

把 GenericAgent 打包为 Docker 镜像，在 Linux 容器中运行（适合云服务器、干净沙箱、多人共享环境）。

## 目录内容

| 文件 | 说明 |
|---|---|
| `Dockerfile` | 镜像构建脚本（Python 3.11-slim） |
| `.dockerignore` | 构建上下文排除规则（密钥/大文件/运行时数据） |
| `docker-compose.yml` | 一键编排（端口、密钥卷、持久化） |
| `docker/entrypoint.sh` | 容器入口（模式切换、密钥检查、记忆种子恢复） |
| `requirements-docker.txt` | 容器内 Python 依赖（核心 + UI 子集） |

## 快速开始

### 1. 准备密钥

```bash
# 在本机（项目根目录）复制模板并填写你的 API 密钥
cp mykey_template.py mykey.py
# 编辑 mykey.py，填入 apikey / api_config 等
```

> 密钥**不会**被打包进镜像（`.dockerignore` 已排除），运行时通过只读卷挂载，安全且便于更换。

### 2. 构建镜像

```bash
docker build -t genericagent:latest .
# 或使用 Compose
docker compose build
```

> 构建上下文已排除 `temp/`（本机运行时数据）与 `assets/demo|images`、技术报告 PDF，镜像约 1 GB 量级（基础镜像 + Streamlit 依赖）。

### 3. 运行

**Web UI（默认，Streamlit）**：

```bash
docker run -d --name ga \
  -p 8501:8501 \
  -v "$(pwd)/mykey.py:/app/mykey.py:ro" \
  genericagent:latest
# 浏览器打开 http://localhost:8501

# Compose 方式
docker compose up -d
```

**终端 TUI（交互式）**：

```bash
docker run -it --rm \
  -v "$(pwd)/mykey.py:/app/mykey.py:ro" \
  genericagent:latest tui
```

**CLI 分发器**（`ga` 子命令，如 `ga list`）：

```bash
docker run -it --rm \
  -v "$(pwd)/mykey.py:/app/mykey.py:ro" \
  genericagent:latest cli list
```

**直接跑 agentmain**：

```bash
docker run -it --rm \
  -v "$(pwd)/mykey.py:/app/mykey.py:ro" \
  genericagent:latest agent --help
```

模式也可用环境变量 `GA_MODE` 指定（命令行参数优先）：`GA_MODE=tui`。

### 4. 持久化（可选）

自进化记忆（`memory/` 含 SOP、技能）与工作目录（`temp/`）默认在容器内，容器删除即丢失。需要持久化时挂载卷：

```bash
mkdir -p ga_memory ga_temp
docker run -d --name ga \
  -p 8501:8501 \
  -v "$(pwd)/mykey.py:/app/mykey.py:ro" \
  -v "$(pwd)/ga_memory:/app/memory" \
  -v "$(pwd)/ga_temp:/app/temp" \
  genericagent:latest
```

首次挂载**空** `ga_memory` 时，入口脚本会自动从镜像内置种子（`/app/memory_seed`）恢复预装 SOP 与工具，无需手工拷贝。

## 镜像内能力边界

| 能力 | 容器内 | 说明 |
|---|---|---|
| 终端 / 文件系统 / 网络 | ✅ | agent 的核心工具可用 |
| Python / pip 安装 | ✅ | 容器隔离提供安全边界，可自主装包 |
| Web UI / TUI / CLI | ✅ | 三种模式均支持 |
| 浏览器注入（TMWebDriver） | ⚠️ | 容器内无浏览器；需自行安装 chromium 并配置扩展/CDP 桥（`assets/tmwd_cdp_bridge`），把浏览器端口暴露给 agent |
| 桌面控制（键鼠/截屏/窗口枚举） | ❌ | 无 X11/桌面；可尝试挂载 X11 socket（Linux 宿主）后使用 |
| ADB（手机） | ❌ | 需 `--device` 透传 USB 设备，配置复杂 |
| pywebview 桌面壳（launch.pyw） | ❌ | 无 GUI，请使用 Web UI 替代 |

## 常见问题

- **启动后报"未配置 API"**：mykey.py 未挂载（容器用的是入口生成的占位文件）。按上文挂载真实密钥后重启。
- **端口冲突**：`-p 8502:8501` 或设置 `PORT=8502`。
- **TUI 乱码/无交互**：必须用 `-it` 运行。
- **想装 IM Bot 前端**：在 `requirements-docker.txt` 追加对应 SDK（如 `python-telegram-bot`、`lark-oapi`）后重新 `docker build`。
- **容器内时区/语言**：默认 UTC / 中文提示（`GA_LANG=zh`）。可用 `-e TZ=Asia/Shanghai` 调整时区。

## 与原生安装的差异

- 无 pywebview 桌面壳、无 IM bot（按需自装）、无 GUI 浏览器注入。
- 记忆种子是构建时从本机 `memory/` 清理后的副本（不含 `L4_raw_sessions/`、`global_mem.txt`、密钥等本机数据）——即"干净出厂状态"，首次使用需要让 agent 重新自进化。
