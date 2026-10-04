FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/tide.db \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 数据库目录（compose 中挂卷；本地 docker run 可用 -v 覆盖）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 启动即建表并（默认）初始化演示数据
CMD ["sh", "-c", "uvicorn app.main:app --host ${HOST} --port ${PORT}"]
