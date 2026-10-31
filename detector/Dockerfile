FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Descarga el modelo al construir para no depender de internet al arrancar.
RUN python -c "from ultralytics import YOLO; YOLO('yolo11n.pt')"
COPY detector.py .
CMD ["python", "-u", "detector.py"]
