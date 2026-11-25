# huta.run: the landing page and agent docs, as an ordinary huta project (huta-site, web recipe).
# Same tiny static-server base as examples/hello. Built on dev only (`huta pack --app site/deploy/app.toml`).
# RELEASE_SHA is the git commit this image was built from; /healthz reports it.

FROM busybox:1.36.1@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662 AS stamp
ARG RELEASE_SHA=unknown
COPY public/ /out/
# Cache-busting: Cloudflare keeps style.css for hours, so every release links its stylesheet
# under a new URL (/style.css?v=<sha>). Found 2026-11-09: a new page with a stale stylesheet.
RUN printf '{"ok":true,"sha":"%s"}\n' "$RELEASE_SHA" > /out/healthz \
 && for f in /out/*.html; do sed -i "s|href=\"/style.css\"|href=\"/style.css?v=${RELEASE_SHA}\"|g" "$f"; done \
 && grep -q "style.css?v=${RELEASE_SHA}" /out/index.html

FROM joseluisq/static-web-server:2.33.0@sha256:04def7ef00cfb0dd7fb869e68a76140ce79cc607f0d5773b873c8d69d4f6fdbf
ARG RELEASE_SHA=unknown
LABEL org.opencontainers.image.revision=$RELEASE_SHA \
      run.huta.project=huta-site
COPY --from=stamp /out/ /public/
COPY sws.toml /etc/sws.toml
ENV SERVER_CONFIG_FILE=/etc/sws.toml
USER 20001:20001
EXPOSE 8080
