# 使用官方python基础镜像，debian自带apt，可以安装ffmpeg
FROM python:3.11-slim

# 安装ffmpeg（自带ffprobe）
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 安装flask
RUN pip install flask

# 复制项目代码到容器内
COPY web.py .
COPY video_cutter.py .
COPY webui ./webui

# 暴露端口（和你web.py监听端口保持一致，这里8770）
EXPOSE 8770

# 启动命令
CMD ["python", "web.py"]
