FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖随镜像构建完成安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite 数据目录（可用卷挂载持久化）
ENV DATABASE_PATH=/data/plans.db \
    PORT=8000
RUN mkdir -p /data

EXPOSE 8000

# 启动时应用会自动完成建库建表
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
