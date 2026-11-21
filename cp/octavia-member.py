"""Ensure that a verified K3s worker is an online Octavia pool member."""

import argparse
import time

import openstack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cloud")
    parser.add_argument("pool_id")
    parser.add_argument("name")
    parser.add_argument("address")
    parser.add_argument("subnet_id")
    args = parser.parse_args()

    lb = openstack.connect(cloud=args.cloud, api_timeout=15).load_balancer
    pool = lb.get_pool(args.pool_id)
    if pool.protocol != "HTTP" or not pool.health_monitor_id:
        raise SystemExit("Expected an HTTP pool with an active health monitor")

    members = list(lb.members(pool))
    matching = [m for m in members if m.address == args.address and m.protocol_port == 80]
    if len(matching) > 1 or any(m.name == args.name and m not in matching for m in members):
        raise SystemExit("Worker name or address conflicts with an existing Octavia member")

    if matching:
        member = matching[0]
        if member.subnet_id != args.subnet_id or not member.is_admin_state_up:
            raise SystemExit("Existing Octavia member has a different subnet or is disabled")
        result = "reused"
    else:
        member = lb.create_member(
            pool, name=args.name, address=args.address, protocol_port=80,
            subnet_id=args.subnet_id,
        )
        result = "created"

    deadline = time.monotonic() + 120
    while True:
        member = lb.get_member(member.id, pool)
        if member.provisioning_status == "ACTIVE" and member.operating_status == "ONLINE":
            print(f"{result} {member.address}:{member.protocol_port} {member.id} ACTIVE ONLINE")
            return
        if member.provisioning_status == "ERROR" or time.monotonic() >= deadline:
            raise SystemExit(
                f"Octavia member {member.id}: {member.provisioning_status}/{member.operating_status}"
            )
        time.sleep(3)


if __name__ == "__main__":
    main()
