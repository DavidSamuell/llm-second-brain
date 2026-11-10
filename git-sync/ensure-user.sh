#!/bin/sh
# ssh and git need a passwd entry for the uid the service runs as (PUID varies per host).
getent passwd "$(id -u)" >/dev/null || echo "git-sync:x:$(id -u):$(id -g)::/data/git-sync:/bin/sh" >> /etc/passwd
exec "$@"
