# git-sync (plan D32): bash + git + git-lfs + flock + inotify-tools + curl + jq.
FROM alpine:3.22@sha256:49340b346798875b5592e5d420241f2f9cfba5f47e62ebb1446a1b34734e898f
# Package versions come from the digest-pinned Alpine release.
# ssh needs a passwd entry for whatever PUID the service runs as; ensure-user adds it at start.
# The image has no setuid/setgid binaries, so a writable /etc/passwd grants nothing more.
# hadolint ignore=DL3018
RUN apk add --no-cache bash git git-lfs flock inotify-tools curl jq openssh-client coreutils findutils \
 && chmod 666 /etc/passwd
COPY git-sync/ensure-user.sh /usr/local/bin/ensure-user
COPY scripts/lib/alert.sh /usr/local/lib/kit/alert.sh
COPY git-sync/sync.sh /usr/local/bin/git-sync
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/ensure-user", "/usr/local/bin/git-sync"]
