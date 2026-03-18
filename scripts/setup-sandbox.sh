#!/bin/bash
# Deploy ~/ask and ~/review into the NemoClaw sandbox.
# Re-run this after sandbox recreation to restore the tools.
#
# Set GATEWAY_NAME and SANDBOX_NAME in your .env or environment.

set -euo pipefail

GATEWAY_NAME="${GATEWAY_NAME:?Set GATEWAY_NAME (e.g. nemoclaw)}"
SANDBOX_NAME="${SANDBOX_NAME:?Set SANDBOX_NAME (e.g. your-sandbox)}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Deploying ~/ask ..."
openshell sandbox upload "$SANDBOX_NAME" "$SCRIPT_DIR/ask" /sandbox/ -g "$GATEWAY_NAME"

echo "Deploying ~/review ..."
openshell sandbox upload "$SANDBOX_NAME" "$SCRIPT_DIR/review" /sandbox/ -g "$GATEWAY_NAME"

echo "Done."
