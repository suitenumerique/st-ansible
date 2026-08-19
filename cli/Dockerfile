# syntax=docker/dockerfile:1
FROM python:3.14-alpine

# shadow (groupadd/useradd) + setpriv (util-linux): the entrypoint uses them to
# align with the bind-mount owner and drop privileges to it.
RUN apk add --no-cache bash git openssh-client ca-certificates \
    shadow setpriv

COPY . /src
RUN <<EOR
set -eu

# Install st-cli with the `full` extra (ansible-core + hvac)
# musllinux wheels mean no compiler is needed.
pip install --no-cache-dir '/src[full]' && rm -rf /src

# ssh/config.local (gitignored, per-operator) Included before ssh/config
# + Accept new HostKeys by default
printf '\nInclude /st-cli/ssh/config.local\nInclude /st-cli/ssh/config\nHost *\n    UserKnownHostsFile /st-cli/ssh/known_hosts\n    StrictHostKeyChecking accept-new\n' >> /etc/ssh/ssh_config
EOR

# Pin ansible-core's ssh control path and local temp under /tmp so they never
# land in the user's bind mount.
ENV ANSIBLE_SSH_CONTROL_PATH_DIR=/tmp/.ansible/cp
ENV ANSIBLE_LOCAL_TEMP=/tmp/.ansible/tmp

# Deployment repo is bind-mounted here: -v $(pwd):/st-cli
WORKDIR /st-cli

# The entrypoint aligns identity with the bind-mount owner (when started as root)
# so written files aren't owned by root, then exec's st-cli. See entrypoint.sh.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["--help"]
