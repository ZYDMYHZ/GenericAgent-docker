# ─────────────────────────────────────────────────────────────────────────────
#  GenericAgent — Docker 镜像
#  说明见 docs/docker_zh.md
#  Python 版本: 3.11 (README 明确支持 3.11/3.12; 3.14 与 pywebview 等不兼容)
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    GA_LANG=zh \
    LANG=C.UTF-8

WORKDIR /app

# 系统依赖: agent 常用 git/curl
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 先装 Python 依赖(利用层缓存)
COPY requirements-docker.txt ./
RUN pip install --no-cache-dir -r requirements-docker.txt

# 复制项目(敏感/大文件由 .dockerignore 排除)
COPY . .

# 生成"记忆种子"目录:
#  - 镜像内保留预装 SOP/工具(memory/*.md, *.py 等干净副本)
#  - 若用户挂载空 volume 到 /app/memory, entrypoint 会从种子自动恢复
RUN rm -rf /app/memory/L4_raw_sessions /app/memory/__pycache__ \
    && rm -f /app/memory/global_mem.txt /app/memory/global_mem_insight.txt \
           /app/memory/file_access_stats.json /app/memory/mykey.py \
    && cp -r /app/memory /app/memory_seed \
    && mkdir -p /app/temp

COPY docker/entrypoint.sh /docker/entrypoint.sh
RUN chmod +x /docker/entrypoint.sh

EXPOSE 8501

ENTRYPOINT ["/docker/entrypoint.sh"]
# 默认 Web UI (Streamlit); 也可: tui / cli / agent
CMD ["streamlit"]
