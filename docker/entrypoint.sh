#!/bin/sh
# GenericAgent 容器入口
# 用法: docker run genericagent [streamlit|tui|cli|agent]
# 或通过环境变量 GA_MODE 指定(参数优先)
set -e
cd /app

echo "[GA] GenericAgent container starting (mode: ${1:-${GA_MODE:-streamlit}}) ..."

# 1. 密钥: llmcore 顶层 import mykey, 文件必须存在
if [ ! -f /app/mykey.py ]; then
    echo "[GA] WARN: /app/mykey.py 不存在, 已生成占位文件。"
    echo "[GA]      请将真实密钥挂载到容器 /app/mykey.py (只读卷), 例如:"
    echo "[GA]      -v \$(pwd)/mykey.py:/app/mykey.py:ro"
    echo "[GA]      参考模板: /app/mykey_template.py (复制为 mykey.py 并填写 apikey)"
    printf '# 占位 mykey.py - 请挂载真实密钥文件(见 docs/docker_zh.md)\n_PLACEHOLDER = True\n' > /app/mykey.py
fi

# 2. 记忆种子: 首次挂载空 volume 时恢复内置 SOP/工具
if [ -d /app/memory_seed ]; then
    if [ ! -d /app/memory ]; then
        mkdir -p /app/memory
    fi
    if [ -z "$(ls -A /app/memory 2>/dev/null)" ]; then
        echo "[GA] 初始化 memory/ (从内置种子恢复 SOP)..."
        cp -rn /app/memory_seed/. /app/memory/
    fi
fi

mkdir -p /app/temp

MODE="${1:-${GA_MODE:-streamlit}}"
case "$MODE" in
  streamlit|web)
    echo "[GA] Web UI: http://localhost:${PORT:-8501}"
    exec python -m streamlit run frontends/stapp.py \
        --server.port "${PORT:-8501}" \
        --server.address 0.0.0.0 \
        --server.headless true \
    ;;
  wehub|hub)
    echo "[GA] Web Hub 管理面板: http://localhost:8901 (token: ${WEHUB_TOKEN:-未设置})"
    echo "[GA] 可一键启动/停止: IM Bot (tg/dc/qq/fs/dingtalk/wecom/wechat), Reflect 服务, Web UI"
    exec python docker/wehub.py --port 8901
    ;;
  tui)
    echo "[GA] TUI 模式(需要 docker run -it)"
    exec python frontends/tui_v3.py
    ;;
  cli|ga)
    shift 2>/dev/null || true
    exec python -m ga_cli "$@"
    ;;
  agent)
    shift 2>/dev/null || true
    exec python agentmain.py "$@"
    ;;
  *)
    echo "[GA] 未知模式: $MODE (可选: streamlit | tui | cli | agent)"
    exit 1
    ;;
esac
