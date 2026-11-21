#!/bin/sh
set -eu

automation_dir=/home/ubuntu/beet-worker-automation
worker_name=beet-k3s-w2
case "${SSH_ORIGINAL_COMMAND:-}" in
    check)
        exec sudo -n k3s kubectl wait --for=condition=Ready "node/$worker_name" --timeout=10s
        ;;
    step)
        cd "$automation_dir"
        exec /home/ubuntu/.local/share/uv/tools/ansible-core/bin/python \
            "$automation_dir/worker-exec.py"
        ;;
    step-preview)
        cd "$automation_dir"
        exec /home/ubuntu/.local/share/uv/tools/ansible-core/bin/python \
            "$automation_dir/worker-exec.py" --preview
        ;;
    *)
        printf '%s\n' 'Allowed commands: check, step, step-preview (one EC2 order on stdin)' >&2
        exit 64
        ;;
esac
