FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制代码
COPY . .

# 创建数据目录
RUN mkdir -p data/uploads data/vectorstore data/logs

EXPOSE 8000

CMD ["python", "main.py"]
