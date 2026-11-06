# The Linux container local_ci.py runs script tests in on macOS (foundation-playbook §8c L2): CI's Python minor version
# (local_ci.CI_PYTHON), bash 5 and the tools the runner image has, so a test that passes on the Mac's bash 3.2 or another
# Python and fails on CI's Linux fails here first. local_ci.py tags the image with the SHA-256 of this file, so a changed
# file is always rebuilt. The base is python:3.14-slim from ECR Public, pinned by digest.
FROM public.ecr.aws/docker/library/python:3.14-slim@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends bash ca-certificates curl git jq shellcheck \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pyyaml==6.0.3 pytest==9.1.1
# local_ci.py runs the container as the laptop user with HOME=/tmp, so `pip install` in a step puts commands in
# /tmp/.local/bin; pytest is baked in at the version python-checks.yml pins (PYTEST_VERSION).
ENV PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1 PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin:/tmp/.local/bin
