# Detector node. Heavier than the others because of torch, so it is its own image.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 HF_HOME=/hf
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install torch==2.15.0 torchvision==0.29.2 --index-url https://download.pytorch.org/whl/cpu
COPY requirements/detector.txt requirements/detector.txt
RUN pip install -r requirements/detector.txt
# The AI models are downloaded now, while the image is built, so starting the detector never needs the internet.
# The names match ACCIDENT_MODEL and VEHICLE_MODEL in src/atms_edge/detector.py. It sits before COPY src so code changes do not download them again.
RUN python -c "from transformers import pipeline; [pipeline('object-detection', model=m) for m in ('hilmantm/detr-traffic-accident-detection', 'PekingU/rtdetr_v2_r18vd')]"
ENV HF_HUB_OFFLINE=1
COPY docker/probe.py /probe.py
COPY src src
ENV PYTHONPATH=/app/src
EXPOSE 8300
CMD ["python", "-m", "atms_edge.service"]
