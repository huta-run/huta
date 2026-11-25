# hello: the smallest huta project. A static page in a tiny static-server image (D-006).
# Built on dev only. RELEASE_SHA is the git commit this image was built from; /healthz reports it.

FROM busybox:1.36.1@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662 AS stamp
ARG RELEASE_SHA=unknown
RUN mkdir /out && printf '{"ok":true,"sha":"%s"}\n' "$RELEASE_SHA" > /out/healthz

FROM joseluisq/static-web-server:2.33.0@sha256:04def7ef00cfb0dd7fb869e68a76140ce79cc607f0d5773b873c8d69d4f6fdbf
ARG RELEASE_SHA=unknown
LABEL org.opencontainers.image.revision=$RELEASE_SHA \
      org.opencontainers.image.source=https://github.com/huta-run/huta \
      run.huta.project=hello
COPY site/ /public/
COPY --from=stamp /out/healthz /public/healthz
COPY sws.toml /etc/sws.toml
ENV SERVER_CONFIG_FILE=/etc/sws.toml
USER 20001:20001
EXPOSE 8080
