FROM python:3.12-slim
WORKDIR /app
COPY . .
ENV DB_PATH=/data/ops.db
EXPOSE 8000
CMD ["python", "run.py"]
