# Core platform and the simulators. One image, started with a different command by each service in compose.yaml.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
# libgomp is what LightGBM (the forecast model) needs at run time
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements/core.txt requirements/core.txt
RUN pip install -r requirements/core.txt
COPY src src
ENV PYTHONPATH=/app/src
EXPOSE 8000 9100 9200
CMD ["python", "-m", "atms_core.main"]
