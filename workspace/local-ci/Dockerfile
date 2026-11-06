# The Linux container local_ci.py runs script tests in on macOS (foundation-playbook §8c L2): bash 5 and the tools the
# runner image has, so a test that passes on the Mac's bash 3.2 and fails on CI's Linux fails here first.
# local_ci.py tags the image with the SHA-256 of this file, so a changed file is always rebuilt.
# Ubuntu 24.04 (public.ecr.aws/ubuntu/ubuntu:24.04), pinned by digest.
FROM public.ecr.aws/ubuntu/ubuntu@sha256:e700eff42bffb30b0aa37029e5a9083745fbb1fa86420711b2c9639b51fc77f7
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      bash ca-certificates curl git jq python3 python3-pip python3-venv python3-yaml shellcheck \
 && rm -rf /var/lib/apt/lists/* \
 && ln -s /usr/bin/python3 /usr/local/bin/python
# The container is throwaway: a step's `pip install` goes to the user's site directory, as on the runner image.
ENV PIP_BREAK_SYSTEM_PACKAGES=1 PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1
