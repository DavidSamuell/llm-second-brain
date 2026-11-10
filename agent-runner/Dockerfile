# agent-runner (plan D30): Claude Code headless on a schedule, talking to vault-mcp only.
FROM node:22-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d
ARG TARGETARCH
ARG CLAUDE_CODE_VERSION=2.1.322
ARG SUPERCRONIC_VERSION=v0.2.51
ARG SUPERCRONIC_SHA256_AMD64=cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123
ARG SUPERCRONIC_SHA256_ARM64=4ae22576a78223d8ccdf5f3b3660a5cbb5288416bf0ca96afc4a36d195932b94
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# Package versions come from the digest-pinned Debian release of the base image.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl jq util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && sha="SUPERCRONIC_SHA256_${TARGETARCH^^}" \
 && curl -fsSL -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}" \
 && echo "${!sha}  /usr/local/bin/supercronic" | sha256sum -c - \
 && chmod +x /usr/local/bin/supercronic \
 && npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force \
 && chmod 666 /etc/passwd
COPY scripts/lib/alert.sh /usr/local/lib/kit/alert.sh
# PUID may not be 1000 (the image's node user): ensure-user adds a passwd entry at start.
COPY git-sync/ensure-user.sh /usr/local/bin/ensure-user
COPY agent-runner/entrypoint.sh /usr/local/bin/agent-runner
COPY agent-runner/run-job.sh /usr/local/bin/run-job
# The kit's skills, used when the vault has none of its own (D30).
COPY vault-template/skills /opt/kit-skills
USER node
ENV HOME=/home/node
ENTRYPOINT ["/usr/local/bin/ensure-user", "/usr/local/bin/agent-runner"]
CMD ["cron"]
