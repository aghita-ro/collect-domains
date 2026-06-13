#!/usr/bin/env bash
# Build the collect-domains Docker image.
# Override the image name with: IMAGE=my-name ./docker_build.sh
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${IMAGE:-collect-domains}"

echo "Building image '$IMAGE' from $(pwd)/Dockerfile ..."
docker build -t "$IMAGE" .
echo "Done. Built '$IMAGE'. Start it with ./docker_run.sh"
