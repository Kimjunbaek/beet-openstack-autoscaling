"""Read the worker's ED25519 host key from its OpenStack console."""

import re
import sys

import openstack


cloud_name, server_id, address = sys.argv[1:]
cloud = openstack.connect(cloud=cloud_name)
console = cloud.compute.get_server_console_output(server_id)["output"]
keys = set(
    re.findall(r"BEET_SSH_HOST_KEY (ssh-ed25519 [A-Za-z0-9+/=]+)(?=\s|$)", console)
)
if len(keys) != 1:
    sys.exit("The VM console does not contain a unique ED25519 host key yet.")

print(f"{address} {keys.pop()}")
