# Detector node. Heavier than the others because of torch, so it is its own image.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 HF_HOME=/hf
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install torch==2.15.0 torchvision==0.29.2 --index-url https://download.pytorch.org/whl/cpu
COPY requirements/detector.txt requirements/detector.txt
RUN pip install -r requirements/detector.txt
COPY docker/probe.py /probe.py
COPY src src
ENV PYTHONPATH=/app/src
EXPOSE 8300
CMD ["python", "-m", "atms_edge.service"]
