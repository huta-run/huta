# notes: the huta v1 example (data, jobs, smoke, egress, upstreams, secrets). Built on dev only.
FROM python:3.13-alpine@sha256:1e1f6334529d1fb26a2ef619d7cffb30a7c7d7cc1cc7338ebee283a58d1aeaca
ARG RELEASE_SHA=unknown
LABEL org.opencontainers.image.revision=$RELEASE_SHA \
      org.opencontainers.image.source=https://github.com/huta-run/huta \
      run.huta.project=notes
COPY app.py /app/app.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 20003:20003
EXPOSE 8080
CMD ["python", "/app/app.py"]
