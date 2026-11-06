# The Linux container local_ci.py runs script tests in on macOS (foundation-playbook §8c L2): CI's Python minor version
# (local_ci.CI_PYTHON), bash 5 and the tools the runner image has, so a test that passes on the Mac's bash 3.2 or another
# Python and fails on CI's Linux fails here first. local_ci.py tags the image with the SHA-256 of this file, so a changed
# file is always rebuilt. The base is python:3.14-slim from ECR Public, pinned by digest.
FROM public.ecr.aws/docker/library/python:3.14-slim@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends bash ca-certificates curl git jq shellcheck \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pyyaml==6.0.3 pytest==9.1.1 uv==0.12.0
# uv (and its uvx) at local_ci.UV_VERSION, as CI installs it: steps such as the contract tests run `uvx`
# (pitfall 28: a container that lacks a tool CI has fails a step CI passes).
# local_ci.py runs the container as the laptop user with HOME=/tmp, so `pip install` in a step puts commands in
# /tmp/.local/bin; pytest is baked in at the version python-checks.yml pins (PYTEST_VERSION).
# terraform at local_ci.TERRAFORM_VERSION (CI's setup-terraform pin): infra steps run scripts that call `terraform init`
# and `terraform test` (render_taskdefs.py, check_iam_boundary.py; T-68). The SHA-256 of each zip is from HashiCorp's
# terraform_<version>_SHA256SUMS, its signature checked against key C874 011F 0AB4 0511 0D02 1055 3436 5D94 72D7 468F.
ARG TARGETARCH
ARG TERRAFORM_VERSION=1.16.6
RUN set -eu; arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) sum=38034b38d03096399a7a1264a14561a8029b2b496fd1f169452656e53968f1b7 ;; \
      arm64) sum=a750b5a11038f165846b2658c40c88cd7070aa67a61bab431a14ba27928ea9ea ;; \
      *) echo "no terraform checksum for $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/terraform.zip "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${arch}.zip"; \
    echo "$sum  /tmp/terraform.zip" | sha256sum -c -; \
    python3 -c "import zipfile; zipfile.ZipFile('/tmp/terraform.zip').extract('terraform', '/usr/local/bin')"; \
    chmod 755 /usr/local/bin/terraform; rm /tmp/terraform.zip; terraform version
ENV PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1 PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin:/tmp/.local/bin
