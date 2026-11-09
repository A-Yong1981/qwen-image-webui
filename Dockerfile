FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# 把靜態檔案的時間戳設成構建時間：COPY 進來的檔案時間戳是 1970，
# 瀏覽器帶 If-Modified-Since: 1970 來問會被誤回 304，舊檔永遠翻新不了。
RUN find app/static -type f -exec touch {} +
RUN mkdir -p /data
EXPOSE 3100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3100"]
