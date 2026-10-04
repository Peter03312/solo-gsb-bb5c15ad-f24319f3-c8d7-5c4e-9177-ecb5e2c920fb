FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# 服务启动时自动执行 CREATE TABLE IF NOT EXISTS 初始化数据库
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
