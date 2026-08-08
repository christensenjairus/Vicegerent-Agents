# syntax=docker/dockerfile:1
# RUN --mount (bind + cache) below needs the BuildKit frontend; pinned to the
# floating v1 tag so it picks up fixes without a manual bump.
# Renovate only learns the new tag name here, not the commit it points to.
# When it bumps HERMES_VERSION, independently look up HERMES_GIT_SHA on
# GitHub (`git ls-remote --tags` or the tag's own page) rather than trusting
# the failing build's own output for it -- that reintroduces the blind trust
# this pin exists to remove.
# renovate: datasource=github-tags depName=NousResearch/hermes-agent
ARG HERMES_VERSION=v2026.8.3
ARG HERMES_GIT_SHA=3c27eb6234bf91b8ceee9e9071591b31e9b148cb

# Also supplies the Go toolchain (/usr/local/go) copied into the final image below.
# renovate: datasource=go depName=golang.org/x/tools/gopls
ARG GOPLS_VERSION=v0.23.0
FROM golang:1.26 AS gopls
ARG GOPLS_VERSION
RUN GOBIN=/out GOTOOLCHAIN=auto go install "golang.org/x/tools/gopls@${GOPLS_VERSION}"

# Every purely-static release binary this image bakes, fetched here instead
# of in the main stage below so a Renovate bump of the Hermes source release
# doesn't invalidate any of these downloads -- none of them depend on Hermes'
# contents. Kept on a plain floating
# tag (no digest pin), matching the gopls/mnemosyne-models builder stages'
# convention below.
#
# Portability note: every tool here except tirith ships a statically-linked
# Go/Rust binary (CGO_ENABLED=0 equivalent), so its glibc/musl ABI is
# irrelevant -- only architecture (TARGETARCH) matters. tirith's release
# asset is named "...-unknown-linux-gnu.tar.gz", i.e. dynamically linked
# against glibc, but nothing in this stage executes it (verification happens
# in the main stage against the real target ABI -- see below). This stage
# uses debian:trixie-slim (glibc 2.41) rather than bookworm-slim (glibc
# 2.36) because the real bazel(1) binary bazelisk downloads below to warm
# its cache IS executed here and is itself glibc-linked. rtk's release
# binary needs glibc >= 2.39, which this stage's base doesn't guarantee to
# match; that's fine because nothing here executes rtk -- the main stage's
# own `rtk --version` check is the real ABI verification. Bumping to trixie
# is extra headroom for the one thing that does execute here (bazelisk's
# real download-and-run).
FROM debian:trixie-slim AS tools
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar unzip \
    && rm -rf /var/lib/apt/lists/*
ARG TARGETARCH

# terraform-ls is downloaded from HashiCorp releases; strip leading v for URL.
# renovate: datasource=github-releases depName=hashicorp/terraform-ls
ARG TERRAFORM_LS_VERSION=v0.39.0
# renovate: datasource=github-releases depName=hashicorp/terraform
ARG TERRAFORM_VERSION=v1.15.8
RUN TFLS_VER="${TERRAFORM_LS_VERSION#v}" \
    && curl -fsSL -o /tmp/terraform-ls.zip \
        "https://releases.hashicorp.com/terraform-ls/${TFLS_VER}/terraform-ls_${TFLS_VER}_linux_${TARGETARCH}.zip" \
    && unzip -o /tmp/terraform-ls.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/terraform-ls \
    && rm /tmp/terraform-ls.zip \
    && TF_VER="${TERRAFORM_VERSION#v}" \
    && curl -fsSL -o /tmp/terraform.zip \
        "https://releases.hashicorp.com/terraform/${TF_VER}/terraform_${TF_VER}_linux_${TARGETARCH}.zip" \
    && unzip -o /tmp/terraform.zip -d /usr/local/bin terraform \
    && chmod +x /usr/local/bin/terraform \
    && rm /tmp/terraform.zip

# terragrunt for formatting HCL/Terraform files in repos that use it.
# renovate: datasource=github-releases depName=gruntwork-io/terragrunt
ARG TERRAGRUNT_VERSION=v1.1.2
RUN curl -fsSL -o /usr/local/bin/terragrunt \
        "https://github.com/gruntwork-io/terragrunt/releases/download/${TERRAGRUNT_VERSION}/terragrunt_linux_${TARGETARCH}" \
    && chmod +x /usr/local/bin/terragrunt

# Bake tirith pre-exec scanner; runtime auto-download would fail behind egress policy.
# renovate: datasource=github-releases depName=sheeki03/tirith
ARG TIRITH_VERSION=v0.3.3
RUN case "${TARGETARCH}" in \
        arm64) TIRITH_ARCH=aarch64 ;; \
        amd64) TIRITH_ARCH=x86_64 ;; \
        *) echo "tirith: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/tirith.tar.gz \
        "https://github.com/sheeki03/tirith/releases/download/${TIRITH_VERSION}/tirith-${TIRITH_ARCH}-unknown-linux-gnu.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/tirith.tar.gz tirith \
    && chmod +x /usr/local/bin/tirith \
    && rm /tmp/tirith.tar.gz

# Bake local repository inspection, formatting, and linting tools. Cluster/cloud
# CLIs belong in MCP servers, not the sandbox image.
# renovate: datasource=github-releases depName=jqlang/jq
ARG JQ_VERSION=1.8.2
# renovate: datasource=github-releases depName=mikefarah/yq
ARG YQ_VERSION=v4.53.3
RUN curl -fsSL -o /usr/local/bin/jq \
        "https://github.com/jqlang/jq/releases/download/jq-${JQ_VERSION}/jq-linux-${TARGETARCH}" \
    && chmod +x /usr/local/bin/jq \
    && curl -fsSL -o /usr/local/bin/yq \
        "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${TARGETARCH}" \
    && chmod +x /usr/local/bin/yq

# Developer tools used directly by the repositories mounted under /workspace.
# renovate: datasource=github-releases depName=mvdan/sh
ARG SHFMT_VERSION=v3.13.1
# renovate: datasource=github-releases depName=sharkdp/fd
ARG FD_VERSION=v10.4.2
# renovate: datasource=github-releases depName=hadolint/hadolint
ARG HADOLINT_VERSION=v2.15.1
# renovate: datasource=github-releases depName=golangci/golangci-lint
ARG GOLANGCI_LINT_VERSION=v2.12.2
RUN case "${TARGETARCH}" in \
        arm64) FD_ARCH=aarch64; HADOLINT_ARCH=arm64 ;; \
        amd64) FD_ARCH=x86_64; HADOLINT_ARCH=x86_64 ;; \
        *) echo "developer tools: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /usr/local/bin/shfmt \
        "https://github.com/mvdan/sh/releases/download/${SHFMT_VERSION}/shfmt_${SHFMT_VERSION}_linux_${TARGETARCH}" \
    && chmod +x /usr/local/bin/shfmt \
    && curl -fsSL -o /tmp/fd.tar.gz \
        "https://github.com/sharkdp/fd/releases/download/${FD_VERSION}/fd-${FD_VERSION}-${FD_ARCH}-unknown-linux-musl.tar.gz" \
    && tar -xz -C /usr/local/bin --strip-components=1 -f /tmp/fd.tar.gz \
        "fd-${FD_VERSION}-${FD_ARCH}-unknown-linux-musl/fd" \
    && rm /tmp/fd.tar.gz \
    && curl -fsSL -o /usr/local/bin/hadolint \
        "https://github.com/hadolint/hadolint/releases/download/${HADOLINT_VERSION}/hadolint-linux-${HADOLINT_ARCH}" \
    && chmod +x /usr/local/bin/hadolint \
    && GOLANGCI_LINT_VER="${GOLANGCI_LINT_VERSION#v}" \
    && curl -fsSL -o /tmp/golangci-lint.tar.gz \
        "https://github.com/golangci/golangci-lint/releases/download/${GOLANGCI_LINT_VERSION}/golangci-lint-${GOLANGCI_LINT_VER}-linux-${TARGETARCH}.tar.gz" \
    && tar -xz -C /usr/local/bin --strip-components=1 -f /tmp/golangci-lint.tar.gz \
        "golangci-lint-${GOLANGCI_LINT_VER}-linux-${TARGETARCH}/golangci-lint" \
    && rm /tmp/golangci-lint.tar.gz

# Bake buf for repos with a protobuf pre-commit toolchain (buf lint/format).
# renovate: datasource=github-releases depName=bufbuild/buf
ARG BUF_VERSION=v1.72.0
RUN case "${TARGETARCH}" in \
        arm64) BUF_ARCH=aarch64 ;; \
        amd64) BUF_ARCH=x86_64 ;; \
        *) echo "buf: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/buf.tar.gz \
        "https://github.com/bufbuild/buf/releases/download/${BUF_VERSION}/buf-Linux-${BUF_ARCH}.tar.gz" \
    && tar -xz -C /tmp -f /tmp/buf.tar.gz buf/bin/buf --strip-components=2 \
    && mv /tmp/buf /usr/local/bin/buf \
    && rm /tmp/buf.tar.gz \
    && chmod +x /usr/local/bin/buf

# Bake kustomize + kubeconform + cerbos + helm so validate.sh and pre-commit hooks
# work offline in the egress-locked sandbox without downloading at runtime.
# renovate: datasource=github-releases depName=kubernetes-sigs/kustomize
ARG KUSTOMIZE_VERSION=v5.8.1
# renovate: datasource=github-releases depName=yannh/kubeconform
ARG KUBECONFORM_VERSION=v0.8.0
# renovate: datasource=github-releases depName=cerbos/cerbos
ARG CERBOS_VERSION=v0.54.0
RUN case "${TARGETARCH}" in \
        arm64) KUSTOMIZE_ARCH=arm64 ;; \
        amd64) KUSTOMIZE_ARCH=amd64 ;; \
        *) echo "kustomize: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/kustomize.tar.gz \
        "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUSTOMIZE_VERSION}/kustomize_${KUSTOMIZE_VERSION}_linux_${KUSTOMIZE_ARCH}.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/kustomize.tar.gz kustomize \
    && rm /tmp/kustomize.tar.gz \
    && curl -fsSL -o /tmp/kubeconform.tar.gz \
        "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-${TARGETARCH}.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/kubeconform.tar.gz kubeconform \
    && rm /tmp/kubeconform.tar.gz \
    && CERBOS_VER="${CERBOS_VERSION#v}" \
    && case "${TARGETARCH}" in \
        arm64) CERBOS_ARCH=arm64 ;; \
        amd64) CERBOS_ARCH=x86_64 ;; \
        *) echo "cerbos: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/cerbos.tar.gz \
        "https://github.com/cerbos/cerbos/releases/download/${CERBOS_VERSION}/cerbos_${CERBOS_VER}_Linux_${CERBOS_ARCH}.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/cerbos.tar.gz cerbos \
    && rm /tmp/cerbos.tar.gz

# Bake helm so scripts/validate.sh and the validate-manifests pre-commit hook can run offline.
# renovate: datasource=github-releases depName=helm/helm
ARG HELM_VERSION=v4.2.3
RUN case "${TARGETARCH}" in \
        arm64) HELM_ARCH=arm64 ;; \
        amd64) HELM_ARCH=amd64 ;; \
        *) echo "helm: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/helm.tar.gz \
        "https://get.helm.sh/helm-${HELM_VERSION}-linux-${HELM_ARCH}.tar.gz" \
    && tar -xz -C /usr/local/bin --strip-components=1 \
        -f /tmp/helm.tar.gz "linux-${HELM_ARCH}/helm" \
    && rm /tmp/helm.tar.gz

# Bake bazelisk + buildifier/buildozer so agents can build/run Bazel targets in
# repos that use Bazel (e.g. k8s-manifests' helm_charts renders), and reformat/edit
# BUILD files, entirely offline in the egress-locked sandbox. bazelisk itself is a thin
# version-selecting launcher; the actual bazel(1) binary it execs is downloaded on first
# use from releases.bazel.build (verified: NOT github.com -- a plain
# `bazelisk version` attempt against the sandbox's own egress proxy gets a hard
# `Forbidden` from releases.bazel.build even though github.com release assets are
# reachable). Pre-warming bazelisk's on-disk cache at build time (BAZELISK_HOME below,
# baked with BAZEL_PINNED_VERSION) means the sandbox never has to dial
# releases.bazel.build at runtime for that one pinned version -- but this build step
# itself needs releases.bazel.build reachable from wherever `docker build` runs (the
# operator's laptop per this README, not the sandbox). Bzlmod dependency resolution
# (bcr.bazel.build, or an internal mirror) still needs its host reachable at *runtime* --
# this bake only guarantees bazel/buildozer/buildifier themselves are present offline.
# renovate: datasource=github-releases depName=bazelbuild/bazelisk
ARG BAZELISK_VERSION=v1.29.0
# renovate: datasource=github-releases depName=bazelbuild/buildtools
ARG BUILDTOOLS_VERSION=v8.5.1
# Pinned Bazel release to pre-warm bazelisk's cache with; keep in sync with the
# .bazelversion of the Bazel repos this sandbox operates against (moveworks + k8s-manifests: 8.7.0).
ARG BAZEL_PINNED_VERSION=8.7.0
ENV BAZELISK_HOME=/opt/hermes/.cache/bazelisk
RUN case "${TARGETARCH}" in \
        arm64) BT_ARCH=arm64 ;; \
        amd64) BT_ARCH=amd64 ;; \
        *) echo "bazelisk/buildtools: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /usr/local/bin/bazelisk \
        "https://github.com/bazelbuild/bazelisk/releases/download/${BAZELISK_VERSION}/bazelisk-linux-${BT_ARCH}" \
    && chmod +x /usr/local/bin/bazelisk \
    && ln -s /usr/local/bin/bazelisk /usr/local/bin/bazel \
    && curl -fsSL -o /usr/local/bin/buildifier \
        "https://github.com/bazelbuild/buildtools/releases/download/${BUILDTOOLS_VERSION}/buildifier-linux-${BT_ARCH}" \
    && chmod +x /usr/local/bin/buildifier \
    && curl -fsSL -o /usr/local/bin/buildozer \
        "https://github.com/bazelbuild/buildtools/releases/download/${BUILDTOOLS_VERSION}/buildozer-linux-${BT_ARCH}" \
    && chmod +x /usr/local/bin/buildozer \
    && mkdir -p "${BAZELISK_HOME}" \
    && USE_BAZEL_VERSION="${BAZEL_PINNED_VERSION}" bazelisk version \
    && test -n "$(find "${BAZELISK_HOME}" -type f -name 'bazel-real' -o -type f -iname 'bazel*' 2>/dev/null | head -1)" \
    && chmod -R a+rX "${BAZELISK_HOME}"

# Bake rtk for rtk-hermes; arch triples differ between arm64 and amd64 release assets.
# renovate: datasource=github-releases depName=rtk-ai/rtk
ARG RTK_VERSION=v0.44.2
RUN case "${TARGETARCH}" in \
        arm64) RTK_TRIPLE=aarch64-unknown-linux-gnu ;; \
        amd64) RTK_TRIPLE=x86_64-unknown-linux-musl ;; \
        *) echo "rtk: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/rtk.tar.gz \
        "https://github.com/rtk-ai/rtk/releases/download/${RTK_VERSION}/rtk-${RTK_TRIPLE}.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/rtk.tar.gz rtk \
    && chmod +x /usr/local/bin/rtk \
    && rm /tmp/rtk.tar.gz

# Offline Mnemosyne + STT model seed for the egress-locked sandbox; preserve HF cache layout.
FROM python:3.14-slim AS mnemosyne-models
# renovate: datasource=pypi depName=huggingface-hub
ARG HUGGINGFACE_HUB_VERSION=1.26.1
RUN pip install --no-cache-dir "huggingface-hub==${HUGGINGFACE_HUB_VERSION}"
RUN python - <<'PY'
from huggingface_hub import hf_hub_download, snapshot_download
hf_hub_download("openbmb/MiniCPM5-1B-GGUF", "MiniCPM5-1B-Q4_K_M.gguf",
                local_dir="/seed/mnemosyne/models")
snapshot_download("qdrant/bge-small-en-v1.5-onnx-q",
                  cache_dir="/seed/cache/fastembed")
# faster-whisper's default stt.local.model ("base") maps to this HF repo
# (see faster_whisper/utils.py _MODELS); baked here since HF_HUB_OFFLINE=1
# blocks the runtime download faster-whisper does on first transcription.
snapshot_download("Systran/faster-whisper-base",
                  cache_dir="/seed/cache/faster-whisper")
PY
# Drop GGUF download metadata, keep HF cache layout, and fail build if the seed is empty.
# chmod here, not in the final stage: a chmod -R over the copied tree there
# forces an overlayfs copy-up of every file, duplicating the whole seed into a
# second multi-hundred-MB layer. COPY --from preserves modes, so doing it once
# here is enough.
RUN rm -rf /seed/mnemosyne/models/.cache \
    && test -f /seed/mnemosyne/models/MiniCPM5-1B-Q4_K_M.gguf \
    && test -d /seed/cache/fastembed/models--qdrant--bge-small-en-v1.5-onnx-q \
    && test -d /seed/cache/faster-whisper/models--Systran--faster-whisper-base \
    && chmod -R a+rX /seed

# Build the SQLite release Hermes requires for WAL-reset safety. Debian 13's
# packaged library is older than 3.51.3, including in the DHI Python base.
# Not Renovate-tracked: the amalgamation version number doesn't map to a
# datasource. Check https://sqlite.org/changes.html for security fixes when
# reviewing this image periodically, and update both values together.
FROM docker.io/library/debian:13.4 AS sqlite_build
ARG SQLITE_AUTOCONF_VERSION=3530400
ARG SQLITE_SHA256=0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c
RUN apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
        build-essential ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && (curl -fsSL --retry 1 --retry-all-errors --connect-timeout 15 --max-time 60 \
        -o /tmp/sqlite.tar.gz \
        "https://sqlite.org/2026/sqlite-autoconf-${SQLITE_AUTOCONF_VERSION}.tar.gz" \
      || curl -fsSL --retry 3 --retry-all-errors --connect-timeout 15 --max-time 120 \
        -o /tmp/sqlite.tar.gz \
        "https://sources.buildroot.net/sqlite/sqlite-autoconf-${SQLITE_AUTOCONF_VERSION}.tar.gz") \
    && printf '%s  %s\n' "${SQLITE_SHA256}" /tmp/sqlite.tar.gz > /tmp/sqlite.sha256 \
    && sha256sum -c /tmp/sqlite.sha256 \
    && tar -xzf /tmp/sqlite.tar.gz -C /tmp \
    && cd "/tmp/sqlite-autoconf-${SQLITE_AUTOCONF_VERSION}" \
    && CFLAGS="-O2 \
        -DSQLITE_ENABLE_FTS3 \
        -DSQLITE_ENABLE_FTS3_PARENTHESIS \
        -DSQLITE_ENABLE_FTS4 \
        -DSQLITE_ENABLE_FTS5 \
        -DSQLITE_ENABLE_RTREE \
        -DSQLITE_ENABLE_GEOPOLY \
        -DSQLITE_ENABLE_COLUMN_METADATA \
        -DSQLITE_ENABLE_UNLOCK_NOTIFY \
        -DSQLITE_ENABLE_DBSTAT_VTAB \
        -DSQLITE_ENABLE_DBPAGE_VTAB \
        -DSQLITE_ENABLE_MATH_FUNCTIONS \
        -DSQLITE_ENABLE_PREUPDATE_HOOK \
        -DSQLITE_ENABLE_SESSION \
        -DSQLITE_SECURE_DELETE \
        -DSQLITE_THREADSAFE=1 \
        -DSQLITE_MAX_VARIABLE_NUMBER=250000" \
        ./configure --prefix=/opt/sqlite-fixed --disable-static \
    && make -j"$(nproc)" \
    && make install

FROM ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie@sha256:b3c543b6c4f23a5f2df22866bd7857e5d304b67a564f4feab6ac22044dde719b AS uv_source
FROM docker.io/library/node:26-bookworm-slim@sha256:9e6f9357d371591e32ab6f2d8a26d63bdd0d17c29eee3f4f3e7e454d9634bf73 AS node_source

# Fetch the exact tagged Hermes source and verify the tag's peeled commit.
# The source tree replaces the prebuilt nousresearch/hermes-agent image while
# retaining the upstream release as the platform's version boundary.
FROM docker.io/library/debian:13.4-slim AS hermes_source
ARG HERMES_VERSION
ARG HERMES_GIT_SHA
RUN apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
        ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 --filter=blob:none --single-branch \
        --branch "${HERMES_VERSION}" \
        https://github.com/NousResearch/hermes-agent.git /src \
    && test "$(git -C /src rev-parse HEAD)" = "${HERMES_GIT_SHA}" \
    && rm -rf \
        /src/.git \
        /src/.github \
        /src/.plans \
        /src/assets \
        /src/docs \
        /src/infographic \
        /src/nix \
        /src/packaging \
        /src/plans \
        /src/tests \
        /src/website \
    && find /src/apps -mindepth 1 -maxdepth 1 ! -name shared -exec rm -rf {} +

# ---------------------------------------------------------------------------
# Final image.
#
# LAYER ORDER IS LOAD-BEARING -- do not reorder casually.
#
# Docker invalidates the cache by prefix: changing instruction N rebuilds N
# through the end of the stage, regardless of layer size. So this stage is
# ordered by how often its input changes, most stable first:
#
#   base-image-coupled OS packages and runtime substrate
#     -> tagged Hermes source + dependency manifests
#     -> big stable COPY --from trees (go toolchain, model seed, bazel cache)
#     -> pinned single binaries from the `tools` stage
#     -> requirements.txt      (Renovate, ~monthly)
#     -> package.json          (Renovate, ~weekly -- claude-code/codex churn)
#     -> patches/              (edited most often, adds ~no bytes)
#
# A re-run COPY --from produces a byte-identical diff, so its digest is
# unchanged and the registry already has it: re-pushing costs nothing. A
# re-run RUN is non-deterministic and re-pushes in full. That asymmetry is
# why the two expensive RUN layers (uv pip, npm) sit as late as possible
# while the far larger COPY trees sit early.
#
# Build-time-only inputs (requirements.txt, package.json, patches/) are bind-
# mounted rather than COPYed, so they never enter a layer and need no cleanup
# `rm`. Cache mounts keep the uv/npm download caches out of the image while
# still speeding up rebuilds.
# ---------------------------------------------------------------------------
FROM dhi.io/python:3.13-debian13-dev
ARG HERMES_GIT_SHA
USER root

LABEL org.opencontainers.image.title="vicegerent-agent" \
      org.opencontainers.image.description="Vicegerent agent built from tagged Hermes source on Docker Hardened Images" \
      org.opencontainers.image.source="https://gitlab.hahomelabs.com/jchristensen/vicegerent-agents" \
      org.opencontainers.image.base.name="dhi.io/python:3.13-debian13-dev"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The DHI dev variant supplies Python, its headers and venv support. Install all
# additional OS packages in this one base-coupled layer: the upstream Hermes
# runtime dependencies plus the generic sandbox's development and diagnostic
# tools. Playwright still owns its browser-specific dependency install below.
RUN apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
        bash \
        ca-certificates \
        cmake \
        coreutils \
        curl \
        dnsutils \
        ffmpeg \
        findutils \
        fzf \
        g++ \
        gcc \
        git \
        grep \
        gzip \
        iproute2 \
        iputils-ping \
        libatomic1 \
        libffi-dev \
        libolm-dev \
        make \
        netcat-openbsd \
        openjdk-21-jre-headless \
        openssh-client \
        passwd \
        procps \
        ripgrep \
        rsync \
        sed \
        shellcheck \
        tar \
        tmux \
        tree \
        vim \
        xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && keytool -help >/dev/null \
    && fzf --version

COPY --from=sqlite_build /opt/sqlite-fixed/lib/libsqlite3.so.3.53.4 /usr/local/lib/
COPY --from=sqlite_build /opt/sqlite-fixed/bin/sqlite3 /usr/local/bin/
RUN ln -sf libsqlite3.so.3.53.4 /usr/local/lib/libsqlite3.so.0 \
    && ln -sf libsqlite3.so.3.53.4 /usr/local/lib/libsqlite3.so \
    && printf '/usr/local/lib\n' > /etc/ld.so.conf.d/000-sqlite-fixed.conf \
    && ldconfig \
    && sqlite3 -version \
    && python3 -c "import sqlite3, sys; v = sqlite3.sqlite_version_info; sys.exit(f'linked SQLite {sqlite3.sqlite_version} still has the WAL-reset bug') if v < (3, 51, 3) else None; db = sqlite3.connect(':memory:'); db.execute(\"CREATE VIRTUAL TABLE docs USING fts5(content, tokenize='trigram')\"); db.execute(\"INSERT INTO docs VALUES ('hermes')\"); sys.exit('SQLite FTS5 trigram self-test failed') if db.execute(\"SELECT count(*) FROM docs WHERE docs MATCH 'erm'\").fetchone()[0] != 1 else None; db.close()"

# s6-overlay supervises the dashboard and per-profile gateways and reaps
# orphaned subprocesses. Keep the checksums aligned with the tagged release.
# Renovate cannot verify these checksums; re-check them against the release's
# published sha256sum.txt when it bumps S6_OVERLAY_VERSION.
ARG TARGETARCH
# renovate: datasource=github-releases depName=just-containers/s6-overlay
ARG S6_OVERLAY_VERSION=v3.2.3.0
ARG S6_OVERLAY_NOARCH_SHA256=b720f9d9340efc8bb07528b9743813c836e4b02f8693d90241f047998b4c53cf
ARG S6_OVERLAY_X86_64_SHA256=a93f02882c6ed46b21e7adb5c0add86154f01236c93cd82c7d682722e8840563
ARG S6_OVERLAY_AARCH64_SHA256=0952056ff913482163cc30e35b2e944b507ba1025d78f5becbb89367bf344581
ARG S6_OVERLAY_SYMLINKS_SHA256=a60dc5235de3ecbcf874b9c1f18d73263ab99b289b9329aa950e8729c4789f0e
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
        amd64) s6_arch="x86_64"; s6_arch_sha="${S6_OVERLAY_X86_64_SHA256}" ;; \
        arm64) s6_arch="aarch64"; s6_arch_sha="${S6_OVERLAY_AARCH64_SHA256}" ;; \
        *) echo "Unsupported TARGETARCH=${TARGETARCH} for s6-overlay" >&2; exit 1 ;; \
    esac; \
    base="https://github.com/just-containers/s6-overlay/releases/download/${S6_OVERLAY_VERSION}"; \
    curl -fsSL --retry 3 -o /tmp/s6-overlay-noarch.tar.xz \
        "${base}/s6-overlay-noarch.tar.xz"; \
    curl -fsSL --retry 3 -o /tmp/s6-overlay-symlinks-noarch.tar.xz \
        "${base}/s6-overlay-symlinks-noarch.tar.xz"; \
    curl -fsSL --retry 3 -o /tmp/s6-overlay-arch.tar.xz \
        "${base}/s6-overlay-${s6_arch}.tar.xz"; \
    { \
        printf '%s  %s\n' "${S6_OVERLAY_NOARCH_SHA256}" /tmp/s6-overlay-noarch.tar.xz; \
        printf '%s  %s\n' "${s6_arch_sha}" /tmp/s6-overlay-arch.tar.xz; \
        printf '%s  %s\n' "${S6_OVERLAY_SYMLINKS_SHA256}" /tmp/s6-overlay-symlinks-noarch.tar.xz; \
    } > /tmp/s6-overlay.sha256; \
    sha256sum -c /tmp/s6-overlay.sha256; \
    tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz; \
    tar -C / -Jxpf /tmp/s6-overlay-arch.tar.xz; \
    tar -C / -Jxpf /tmp/s6-overlay-symlinks-noarch.tar.xz; \
    rm /tmp/s6-overlay-*.tar.xz /tmp/s6-overlay.sha256

RUN groupadd --gid 10000 agent \
    && useradd --uid 10000 --gid 10000 --create-home --home-dir /opt/data --shell /bin/sh agent \
    && test "$(getent passwd 10000 | cut -d: -f1,6)" = "agent:/opt/data" \
    && ! getent passwd hermes >/dev/null

COPY --chmod=0755 --from=hermes_source /src/docker/tini-shim.sh /usr/bin/tini

COPY --chmod=0755 --from=uv_source /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/
COPY --chmod=0755 --from=node_source /usr/local/bin/node /usr/local/bin/
COPY --from=node_source /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

WORKDIR /opt/hermes

# Install the tagged source using the same dependency and frontend sequence as
# its published image. Separate manifest copies preserve the expensive caches.
COPY --from=hermes_source /src/package.json /src/package-lock.json ./
COPY --from=hermes_source /src/web/package.json web/
COPY --from=hermes_source /src/ui-tui/package.json ui-tui/
COPY --from=hermes_source /src/ui-tui/packages/hermes-ink/ ui-tui/packages/hermes-ink/
COPY --from=hermes_source /src/apps/shared/ apps/shared/
ENV npm_config_install_links=false
RUN npm ci --prefer-offline --no-audit --fetch-retries=5 \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

COPY --from=hermes_source \
    /src/plugins/platforms/photon/sidecar/package.json \
    /src/plugins/platforms/photon/sidecar/package-lock.json \
    /src/plugins/platforms/photon/sidecar/patch-spectrum-mixed-attachments.mjs \
    plugins/platforms/photon/sidecar/
RUN cd plugins/platforms/photon/sidecar \
    && npm ci --no-audit --fetch-retries=5 \
    && npm cache clean --force

COPY --from=hermes_source /src/pyproject.toml /src/uv.lock ./
RUN touch ./README.md \
    && uv sync --frozen --no-install-project \
        --extra all \
        --extra messaging \
        --extra otlp \
        --extra anthropic \
        --extra bedrock \
        --extra azure-identity \
        --extra hindsight \
        --extra matrix

COPY --from=hermes_source /src/web/ web/
COPY --from=hermes_source /src/ui-tui/ ui-tui/
COPY --from=hermes_source /src/apps/shared/ apps/shared/
RUN cd web \
    && npm run build \
    && cd ../ui-tui \
    && npm run build

COPY --link --chmod=a+rX,go-w --from=hermes_source /src/ .
RUN uv pip install --no-cache-dir --no-deps -e "." \
    && mkdir -p /opt/hermes/bin \
    && printf 'docker\n' > /opt/hermes/.install_method \
    && printf '%s\n' "${HERMES_GIT_SHA}" > /opt/hermes/.hermes_build_sha

COPY --from=hermes_source /src/docker/s6-rc.d/ /etc/s6-overlay/s6-rc.d/
RUN mkdir -p /etc/cont-init.d \
    && printf '#!/command/with-contenv sh\nexec /opt/hermes/docker/stage2-hook.sh\n' \
        > /etc/cont-init.d/01-hermes-setup \
    && chmod +x /etc/cont-init.d/01-hermes-setup
COPY --chmod=0755 --from=hermes_source /src/docker/cont-init.d/015-supervise-perms /etc/cont-init.d/015-supervise-perms
COPY --chmod=0755 --from=hermes_source /src/docker/cont-init.d/02-reconcile-profiles /etc/cont-init.d/02-reconcile-profiles
COPY --chmod=0755 --from=hermes_source /src/docker/hermes-exec-shim.sh /opt/hermes/bin/hermes
COPY --chmod=0755 --from=hermes_source /src/docker/entrypoint-dispatch.sh /opt/hermes/docker/entrypoint-dispatch.sh

ENV HOME=/opt/data \
    HERMES_WEB_DIST=/opt/hermes/hermes_cli/web_dist \
    HERMES_TUI_DIR=/opt/hermes/ui-tui \
    HERMES_HOME=/opt/data/.hermes \
    HERMES_WRITE_SAFE_ROOT=/opt/data \
    HERMES_DISABLE_LAZY_INSTALLS=1 \
    HERMES_LAZY_INSTALL_TARGET=/opt/data/.hermes/lazy-packages \
    PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:${PATH}"
RUN mkdir -p /opt/data/.hermes /opt/data/skills \
    && chown -R 10000:10000 /opt/data
VOLUME [ "/opt/data" ]

# requirements.txt tools (pre-commit, detect-secrets, yamllint) live in the
# Hermes venv but are not on PATH upstream. The Docker ENV covers every process;
# the profile.d entry preserves the same ordering for base variants that source
# it. The exec shim must stay ahead of the real venv binary so root invocations
# drop to the runtime account before writing Hermes state.
#
# Shadow low-frequency Bash builtins that have a real coreutils/util-linux
# equivalent so invoking them triggers a genuine execve() -- Tetragon's
# process_exec hook only sees real syscalls, so builtins (echo, printf, etc.)
# run in-process and are otherwise invisible in VictoriaLogs. Keep [` and
# test builtins: ordinary shell control flow executes them frequently and does
# not provide useful process-start telemetry. Covers 6 shadowable builtins;
# state-mutating ones (cd, export, eval, set, ...) have no binary equivalent
# and can't be covered this way. BASH_ENV (not bash.bashrc) is required
# because Hermes' terminal() tool runs plain non-interactive, non-login
# `bash -c` -- the one shell-invocation mode that sources neither
# /etc/profile nor /etc/bash.bashrc, only $BASH_ENV. It's a Docker ENV, so
# every process in the container inherits it, including kubectl exec
# sessions -- no bash.bashrc fallback needed. Accepted tradeoff: external
# pwd resolves symlinks (physical path) where the builtin preserves the
# logical $PWD, and external kill can't take job specs (%1) -- global
# BASH_ENV shadowing means every bash -c in the sandbox gets this, not just
# interactive sessions.
#
# Bundled skill files are installed 444 (root-owned in the image); make them
# 644 so skill_manager_tool can patch them after copytree seeds /opt/data/skills.
# This chmod duplicates every SKILL.md into this layer (a metadata change
# forces overlayfs copy-up), so it belongs here with the other base-image-only
# work rather than after anything that changes more often.
ENV BASH_ENV=/etc/bash-shadow-builtins.sh
COPY --chmod=0644 bash-prompt.sh /etc/vicegerent-bash-prompt.sh
RUN printf 'export PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:$PATH"\n' > /etc/profile.d/00-agent-venv-path.sh \
    && chmod 644 /etc/profile.d/00-agent-venv-path.sh \
    && printf 'enable -n echo false kill printf pwd true\n' > /etc/bash-shadow-builtins.sh \
    && BASH_ENV=/etc/bash-shadow-builtins.sh bash -c '[[ $(type -t "[") == builtin && $(type -t test) == builtin ]]' \
    && chmod 644 /etc/bash-shadow-builtins.sh \
    && printf '\n# Vicegerent interactive Bash prompt (tmux starts non-login Bash).\n[ -r /etc/vicegerent-bash-prompt.sh ] && . /etc/vicegerent-bash-prompt.sh\n' >> /etc/bash.bashrc \
    && find /opt/hermes/skills -name "SKILL.md" -exec chmod 644 {} +

# Full Go toolchain so agents can build/test Go code (e.g. mcp-cerbos-shim).
# Large but changes only with the golang builder stage's FROM.
COPY --from=gopls /usr/local/go /usr/local/go
COPY --from=gopls /out/gopls /usr/local/go/bin/gopls
ENV PATH="/usr/local/go/bin:${PATH}" \
    GOPROXY=https://proxy.golang.org,direct \
    GOSUMDB=sum.golang.org \
    GOTOOLCHAIN=local
RUN printf 'export PATH="/usr/local/go/bin:$PATH"\n' >> /etc/profile.d/00-agent-venv-path.sh \
    && go version

# Bake the Mnemosyne + STT model seed outside /opt/data so the data PVC cannot shadow it.
# The largest single tree in the image and effectively never changes. Modes are
# set in the mnemosyne-models stage, not here: a `chmod -R` in this stage would
# copy the whole multi-hundred-MB tree up into a second layer for nothing.
COPY --from=mnemosyne-models /seed /opt/hermes/mnemosyne-seed
RUN test -f /opt/hermes/mnemosyne-seed/mnemosyne/models/MiniCPM5-1B-Q4_K_M.gguf \
    && test -d /opt/hermes/mnemosyne-seed/cache/fastembed/models--qdrant--bge-small-en-v1.5-onnx-q \
    && test -d /opt/hermes/mnemosyne-seed/cache/faster-whisper/models--Systran--faster-whisper-base \
    && find /opt/hermes/mnemosyne-seed -type f -exec sha256sum {} + \
      | sort -k2 | sha256sum | cut -d' ' -f1 > /opt/hermes/mnemosyne-seed.sha256

# bazelisk + buildifier/buildozer binaries and the pre-warmed bazelisk cache
# (BAZELISK_HOME, baked in the `tools` stage against BAZEL_PINNED_VERSION --
# see there) are copied verbatim; COPY --from preserves modes, so no
# `chmod -R` here avoids duplicating the whole bazel cache. Pinned to the
# target repos' .bazelversion, so it moves about as rarely as the model seed.
ENV BAZELISK_HOME=/opt/hermes/.cache/bazelisk
COPY --from=tools "${BAZELISK_HOME}" "${BAZELISK_HOME}"
COPY --from=tools /usr/local/bin/bazelisk /usr/local/bin/bazelisk
COPY --from=tools /usr/local/bin/buildifier /usr/local/bin/buildifier
COPY --from=tools /usr/local/bin/buildozer /usr/local/bin/buildozer
RUN ln -s /usr/local/bin/bazelisk /usr/local/bin/bazel \
    && buildifier --version \
    && buildozer -version \
    && test -n "$(find "${BAZELISK_HOME}" -type f -name 'bazel-real' -o -type f -iname 'bazel*' 2>/dev/null | head -1)"

# Every remaining statically-linked binary from the independent `tools` stage,
# in one COPY group with one verify layer. All are pinned by an ARG up there, so
# a Renovate bump of any one of them invalidates from here down -- which is why
# this sits above nothing but the pip/npm/patch layers. tirith's release asset is
# glibc-linked, so its `--version` here is the real ABI check against this base.
COPY --from=tools \
    /usr/local/bin/buf \
    /usr/local/bin/cerbos \
    /usr/local/bin/fd \
    /usr/local/bin/golangci-lint \
    /usr/local/bin/hadolint \
    /usr/local/bin/helm \
    /usr/local/bin/jq \
    /usr/local/bin/kubeconform \
    /usr/local/bin/kustomize \
    /usr/local/bin/rtk \
    /usr/local/bin/shfmt \
    /usr/local/bin/terraform \
    /usr/local/bin/terraform-ls \
    /usr/local/bin/terragrunt \
    /usr/local/bin/tirith \
    /usr/local/bin/yq \
    /usr/local/bin/
RUN buf --version \
    && cerbos --version \
    && fd --version \
    && golangci-lint version \
    && hadolint --version \
    && helm version \
    && jq --version \
    && kubeconform -v \
    && kustomize version \
    && rtk --version \
    && shfmt --version \
    && terraform version \
    && tirith --version \
    && yq --version

# cluster/cloud CLIs belong in MCP servers, not the sandbox image: shadow
# their conventional binary names with a stub that redirects an agent to the
# vMCP server instead of a bare "command not found". binaries.txt is the
# single list to extend when a new toolhive-servers.json backend gets an
# obvious CLI equivalent.
COPY vmcp-redirect/stub.sh /usr/local/bin/vmcp-redirect-stub
RUN --mount=type=bind,source=vmcp-redirect/binaries.txt,target=/tmp/vmcp-redirect-binaries.txt \
    chmod +x /usr/local/bin/vmcp-redirect-stub \
    && while IFS= read -r name; do \
        name="${name%%#*}"; \
        name="$(echo "$name" | xargs)"; \
        [ -z "$name" ] && continue; \
        ln -sf /usr/local/bin/vmcp-redirect-stub "/usr/local/bin/${name}"; \
    done < /tmp/vmcp-redirect-binaries.txt \
    && kubectl 2>&1 | grep -q "use the vMCP server instead"

# Client-side protected-branch guard, root-owned on the read-only rootfs so uid
# 10000 cannot edit either half. Both layers are load-bearing: /etc/gitconfig is
# system scope, which git ranks BELOW the agent-writable worktree/local/global/XDG
# config, so the wrapper re-injects core.hooksPath at command scope to keep the hook
# armed; the wrapper alone is bypassed by calling /usr/bin/git directly.
# core.hooksPath replaces .git/hooks wholesale, so hook-dispatch.sh chains to the
# repo's own hook -- else this would silently disable every repo's pre-commit.
COPY git-guard/git /usr/local/bin/git
COPY git-guard/hook-dispatch.sh /opt/vicegerent/git-hooks/hook-dispatch.sh
COPY git-guard/gitconfig /etc/gitconfig
RUN --mount=type=bind,source=git-guard,target=/tmp/git-guard \
    chmod 755 /usr/local/bin/git /opt/vicegerent/git-hooks/hook-dispatch.sh \
    && chmod 444 /etc/gitconfig \
    && for h in pre-push pre-commit prepare-commit-msg commit-msg post-commit \
        post-checkout post-merge post-rewrite pre-rebase pre-merge-commit \
        pre-applypatch post-applypatch applypatch-msg push-to-checkout \
        post-index-change reference-transaction; do \
        ln -sf hook-dispatch.sh "/opt/vicegerent/git-hooks/${h}"; \
    done \
    && for h in /opt/vicegerent/git-hooks/*; do test -e "$h" || { echo "dangling hook symlink: $h" >&2; exit 1; }; done \
    && git config --show-origin core.hooksPath | grep -q /opt/vicegerent/git-hooks \
    && grep -q '^readonly TRUSTED_HOOKS=/opt/vicegerent/git-hooks$' /usr/local/bin/git \
    && bash /tmp/git-guard/test-git-guard.sh /tmp/git-guard

# hermes-lcm must live in Hermes' bundled context_engine tree, not ~/.hermes/plugins.
# renovate: datasource=github-releases depName=stephenschoettler/hermes-lcm
ARG HERMES_LCM_VERSION=v0.20.0
RUN LCM_DEST="$(/opt/hermes/.venv/bin/python -c 'import plugins.context_engine as p, os; print(os.path.dirname(p.__file__))')/lcm" \
    && mkdir -p "$LCM_DEST" \
    && curl -fsSL "https://github.com/stephenschoettler/hermes-lcm/archive/refs/tags/${HERMES_LCM_VERSION}.tar.gz" \
        | tar -xz -C "$LCM_DEST" --strip-components=1 \
    && /opt/hermes/.venv/bin/python -c "from plugins.context_engine import discover_context_engines as d; assert 'lcm' in [n for n, _, _ in d()], 'lcm engine not discovered after bake'; print('hermes-lcm ${HERMES_LCM_VERSION} baked + discoverable')"

# Runtime Python deps for the egress-locked sandbox; Renovate tracks requirements.txt.
# Second-largest RUN layer in the image and re-pushed in full whenever any pin
# moves, so it sits below everything that changes less often. The cache mount
# keeps uv's download cache out of the image while still making rebuilds fast.
RUN --mount=type=bind,source=requirements.txt,target=/tmp/requirements.txt \
    --mount=type=cache,id=vicegerent-agent-uv-v1,target=/root/.cache/uv,sharing=locked \
    uv pip install --python /opt/hermes/.venv/bin/python -r /tmp/requirements.txt

# Global npm CLIs used by the bundled agent harnesses; package.json is the Renovate-tracked source.
# The most frequently bumped large input in the image (claude-code, codex, and
# opencode all move weekly or faster), so it is deliberately the last big layer:
# only the patch layer below it can be invalidated by an npm pin change.
RUN --mount=type=bind,source=package.json,target=/tmp/npm/package.json \
    --mount=type=cache,target=/root/.npm \
    cd /tmp/npm \
    && npm install --global --no-fund --no-audit \
        $(node -p "Object.entries(require('./package.json').dependencies).map(([k,v])=>k+'@'+v).join(' ')") \
    && opencode --version

# Codex has no native LSP client. Bake the standalone runtime from codex-lsp so
# its stdio MCP server uses the image's language servers without runtime egress.
# Pinned to the revision codex-lsp v0.2.0 embeds as its lsp-tools-mcp submodule.
ARG CODEX_LSP_TOOLS_MCP_REVISION=e7c65b04d0cc549f0478d3b78b51714fc0f572b3
RUN --mount=type=cache,target=/root/.cache/codex-lsp \
    mkdir -p /tmp/codex-lsp/node_modules/@types /opt/vicegerent/codex-lsp \
    && curl -fsSL "https://github.com/code-yeongyu/lsp-tools-mcp/archive/${CODEX_LSP_TOOLS_MCP_REVISION}.tar.gz" \
        | tar -xz -C /tmp/codex-lsp --strip-components=1 \
    && ln -s /usr/local/lib/node_modules/@types/node /tmp/codex-lsp/node_modules/@types/node \
    && tsc -p /tmp/codex-lsp/tsconfig.build.json \
    && cp -a /tmp/codex-lsp/dist/. /opt/vicegerent/codex-lsp/ \
    && node /opt/vicegerent/codex-lsp/cli.js mcp </dev/null \
    && rm -rf /tmp/codex-lsp

# Claude Code reads LSP wiring only from plugins, so the image's language servers
# reach it through a sandbox-local marketplace rather than settings.json. The
# agent chart points extraKnownMarketplaces at this directory; it stays read-only
# at runtime because Claude Code caches plugin state under CLAUDE_CONFIG_DIR.
COPY claude-lsp/ /opt/vicegerent/claude-lsp/
RUN chmod +x /opt/vicegerent/claude-lsp/bin/typescript-lsp \
    && sh -n /opt/vicegerent/claude-lsp/bin/typescript-lsp \
    && claude plugin validate /opt/vicegerent/claude-lsp \
    && node -e "for (const [n, s] of Object.entries(require('/opt/vicegerent/claude-lsp/plugins/sandbox-lsp/.lsp.json'))) { \
        const bin = s.command.startsWith('/') ? s.command : require('child_process').execFileSync('sh', ['-c', 'command -v ' + s.command]).toString().trim(); \
        require('fs').accessSync(bin, require('fs').constants.X_OK); \
        console.log('lsp ok: ' + n + ' -> ' + bin); \
    }"

# Claude Code serializes the optimizer's generic call_tool requests. This local
# stdio adapter keeps the normal vMCP path and adds one bounded concurrent batch.
COPY vmcp-bridge/vmcp-stdio-bridge.py /usr/local/bin/vmcp-stdio-bridge
RUN --mount=type=bind,source=vmcp-bridge,target=/tmp/vmcp-bridge \
    chmod +x /usr/local/bin/vmcp-stdio-bridge \
    && /opt/hermes/.venv/bin/python -m py_compile /usr/local/bin/vmcp-stdio-bridge \
    && VMCP_BRIDGE_UNDER_TEST=/usr/local/bin/vmcp-stdio-bridge \
        /opt/hermes/.venv/bin/python /tmp/vmcp-bridge/test-vmcp-stdio-bridge.py

# Publish Hermes' skills to the other harnesses, and snapshot the tree so content
# deleted through a published symlink stays recoverable. Baked in rather than
# mounted as a ConfigMap: this is agent-image behaviour, not cluster config, and a
# ConfigMap costs four wiring points per script in the agent chart. Placed here
# with patches for the same reason -- edited often, adds almost no bytes.
COPY skills-scripts/sync-shared-skills.sh skills-scripts/snapshot-skills.sh \
     /usr/local/bin/
RUN chmod +x /usr/local/bin/sync-shared-skills.sh /usr/local/bin/snapshot-skills.sh \
    && bash -n /usr/local/bin/sync-shared-skills.sh \
    && bash -n /usr/local/bin/snapshot-skills.sh

COPY home-scripts/migrate-hermes-home.sh /usr/local/bin/migrate-hermes-home
RUN chmod +x /usr/local/bin/migrate-hermes-home \
    && bash -n /usr/local/bin/migrate-hermes-home

# Every Vicegerent patch to upstream Hermes, applied in patches/order.txt order
# by patches/apply-patches.sh. Each patch's own module docstring carries its
# rationale, its upstream-removal condition, and its ordering dependencies;
# order.txt records the apply order and why it is not plain numeric order.
#
# Deliberately one layer for the whole directory, and deliberately last: patch
# edits are the most common change to this image, they add almost no bytes, and
# bind-mounting the directory means a patch edit re-pushes only this layer.
# apply-patches.sh fails the build if any patches/[0-9]*.py is not listed in
# order.txt, so a new patch cannot be silently skipped.
RUN --mount=type=bind,source=patches,target=/tmp/patches \
    /opt/hermes/.venv/bin/python /tmp/patches/tests/test_0052_agent_runtime_identity.py \
    && sh /tmp/patches/apply-patches.sh /tmp/patches /opt/hermes/.venv/bin/python \
    && cp -a /opt/hermes/docker/s6-rc.d/. /etc/s6-overlay/s6-rc.d/ \
    && cp -a /opt/hermes/docker/cont-init.d/015-supervise-perms /etc/cont-init.d/015-supervise-perms \
    && cp -a /opt/hermes/docker/cont-init.d/02-reconcile-profiles /etc/cont-init.d/02-reconcile-profiles \
    && cp -a /opt/hermes/docker/hermes-exec-shim.sh /opt/hermes/bin/hermes \
    && chmod 0755 /etc/cont-init.d/015-supervise-perms /etc/cont-init.d/02-reconcile-profiles /opt/hermes/bin/hermes

ENTRYPOINT [ "/opt/hermes/docker/entrypoint-dispatch.sh" ]
CMD [ ]
