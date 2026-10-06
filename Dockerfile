FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser && mkdir -p /data && chown -R appuser:appuser /data /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser start.sh ./start.sh
RUN chmod +x /app/start.sh
USER appuser
EXPOSE 3000
CMD ["/app/start.sh"]
