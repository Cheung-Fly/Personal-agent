FROM python:3.12-slim
WORKDIR /app
# git 给 Agent 做代码操作用；按需增减
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY static ./static
RUN useradd -m agent && mkdir -p /app/agent_workspace && chown -R agent:agent /app
USER agent
ENV WORKSPACE_DIR=/app/agent_workspace
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
