# Dockerfile patterns

**Enforced by the host:** a non-root `USER`, `linux/amd64`, the labels
`org.opencontainers.image.revision=<sha>` and `run.huta.project=<project>`, and `GET /healthz`
returning `{"ok":true,"sha":"<full sha>"}`. `huta pack` takes care of the platform (it always
builds `--platform linux/amd64 --provenance=false`, emulated on arm64 machines) and the labels.

**What the runtime needs:** the app must work with a read-only root filesystem (only `/tmp`
writable) and without outbound network.

**Recommended patterns** (not checked): `ARG RELEASE_SHA` to stamp `/healthz` (`huta pack` passes
`--build-arg RELEASE_SHA=<full sha>`), a numeric `USER`, `LABEL` lines for the two labels so a
plain `docker build` matches, base images pinned by digest (`image:tag@sha256:...`). The
digests below are the ones huta's own example uses.

## 1. Static site (about 14 MB)

A tiny static server ([static-web-server](https://static-web-server.net/) 2.33) plus your files
plus your own HTTP rules. `/healthz` is a file stamped at build time in a busybox stage.

Layout:

```
Dockerfile
sws.toml
site/            your built files (index.html, ...)
deploy/app.toml
```

`Dockerfile`:

```dockerfile
FROM busybox:1.36.1@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662 AS stamp
ARG RELEASE_SHA=unknown
RUN mkdir /out && printf '{"ok":true,"sha":"%s"}\n' "$RELEASE_SHA" > /out/healthz

FROM joseluisq/static-web-server:2.33.0@sha256:04def7ef00cfb0dd7fb869e68a76140ce79cc607f0d5773b873c8d69d4f6fdbf
ARG RELEASE_SHA=unknown
LABEL org.opencontainers.image.revision=$RELEASE_SHA \
      run.huta.project=myapp
COPY site/ /public/
COPY --from=stamp /out/healthz /public/healthz
COPY sws.toml /etc/sws.toml
ENV SERVER_CONFIG_FILE=/etc/sws.toml
USER 20001:20001
EXPOSE 8080
```

`sws.toml`:

```toml
[general]
host = "0.0.0.0"
port = 8080
root = "/public"
log-level = "info"
compression = true
cache-control-headers = true
directory-listing = false

[advanced]

[[advanced.headers]]
source = "**"
[advanced.headers.headers]
X-Content-Type-Options = "nosniff"
Referrer-Policy = "strict-origin-when-cross-origin"

[[advanced.headers]]
source = "/healthz"
[advanced.headers.headers]
Content-Type = "application/json"
Cache-Control = "no-store"
```

`deploy/app.toml`:

```toml
schema = 1
project = "myapp"
order = ["web"]

[images.web]
dockerfile = "Dockerfile"
context = "."

[components.web]
image = "web"
kind = "http"
port = 8080
health = "/healthz"

[state]
kind = "none"
```

If your site needs a build step (a bundler, a static-site generator), add a build stage before
`stamp` and `COPY --from=build /app/dist/ /public/` instead of `COPY site/`.

## 2. Small HTTP service (Python, standard library)

The host also sets `RELEASE_SHA` in the environment at run time (same value), so reading it
from the environment is correct both locally and on the host.

`Dockerfile`:

```dockerfile
# Pin this by digest: python:3.12-slim@sha256:<digest>
FROM python:3.12-slim
ARG RELEASE_SHA=unknown
ENV RELEASE_SHA=$RELEASE_SHA \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
LABEL org.opencontainers.image.revision=$RELEASE_SHA \
      run.huta.project=myapp
WORKDIR /app
COPY app.py /app/app.py
USER 20001:20001
EXPOSE 8080
CMD ["python", "/app/app.py"]
```

`app.py`:

```python
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SHA = os.environ.get("RELEASE_SHA", "unknown")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            body = json.dumps({"ok": True, "sha": SHA}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
        else:
            body = b"hello\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
```

Install dependencies in a build stage (`pip install --target` or a venv copied in); nothing can
be installed at run time.

## Advice for any image

- Report `"ok": true` only when the service can serve. huta switches traffic to the new
  release only after `/healthz` passes, within 30 s of start.
- Write only to `/tmp` (16 MB, lost on restart). There is no persistent storage in v0.
- Make application files world-readable: the container runs as the project's uid from the
  host registry, which may differ from your `USER`.
- Handle SIGTERM. After the switch the old release keeps running for the target's `drain`
  seconds (default 10), then gets SIGTERM and 10 s more before it is killed. Long requests,
  streams and websockets on the old release can be cut.
- No secrets in build args, layers or files. Secrets reach the app as environment variables at
  run time ([Runtime](runtime.md#secrets-and-environment)).
- `huta pack` builds from `git archive HEAD`, not your working tree: only committed files
  exist in the build. Generated files (a built `dist/`, `node_modules`) must be produced by a
  build stage in the Dockerfile, not copied from your disk.
- Listen on `0.0.0.0` at the port in `app.toml`. A server bound to `127.0.0.1` inside the
  container is unreachable from the host's check; the release ends `nothing-activated` with
  `never reported <sha12>`.
- Test the committed state as the host will run it: take the sha in the repo, export the
  commit with `git archive` (like `huta pack`), build from the export:

  ```sh
  sha=$(git rev-parse HEAD)
  rm -rf /tmp/myapp-export && mkdir /tmp/myapp-export
  git archive HEAD | tar -x -C /tmp/myapp-export
  cd /tmp/myapp-export
  docker build --platform linux/amd64 --build-arg RELEASE_SHA="$sha" -t myapp-test .
  docker run --rm -d --name myapp-test -p 8080:8080 --read-only --tmpfs /tmp:size=16m \
    --cap-drop ALL --security-opt no-new-privileges --user 20001:20001 myapp-test
  curl -s localhost:8080/healthz      # must print {"ok":true,"sha":"<$sha>"}
  docker rm -f myapp-test
  cd -
  ```

  To prove the app starts without outbound network, run it once more with `--network none`
  (no port is reachable then; check the logs instead).
