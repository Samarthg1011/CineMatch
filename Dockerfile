# cinematch: Flask API + static UI. Only the runtime needs flask + gunicorn; the model
# artifacts in data/ are prebuilt, so pandas/scikit-learn stay out of the image.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN pip install --no-cache-dir "flask>=3.0" "gunicorn>=21.2"
COPY . .
EXPOSE 8000
# One worker + threads: live-search additions are kept in memory, so workers must share a process.
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:8000", "--timeout", "30", "app:app"]
HEALTHCHECK --interval=30s --timeout=5s --retries=3   CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4).status==200 else 1)"
