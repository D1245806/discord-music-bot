FROM python:3.11-slim

# 安裝 ffmpeg
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean

# 建立工作目錄
WORKDIR /app

# 複製需求套件
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 複製所有程式
COPY . /app

# 啟動 bot
CMD ["python", "musicbot.py"]
