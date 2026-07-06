#!/usr/bin/env python3
"""Reboot (or stop/start) the EC2 instance that hosts this project.

Useful when a deploy wedges the box (e.g. an OOM during a no-cache Docker
build makes SSH unresponsive). This shells out to the already-configured
`aws` CLI, so no extra Python dependencies are required.

Configuration (via app/.env or the environment):
    EC2_INSTANCE_ID   The instance to act on (e.g. i-0abc123...). Required
                      unless passed with --instance-id.
    AWS_REGION        Optional. Falls back to your aws CLI default region.
    AWS_PROFILE       Optional. Falls back to your aws CLI default profile.
    WEBHOOK_BASE_URL  Optional. Used for the post-reboot /health probe.

Usage:
    python scripts/reboot_instance.py status     # show instance + status checks
    python scripts/reboot_instance.py reboot      # graceful reboot (default)
    python scripts/reboot_instance.py stop        # stop the instance
    python scripts/reboot_instance.py start       # start the instance
    python scripts/reboot_instance.py reboot --wait        # wait until running
    python scripts/reboot_instance.py reboot --instance-id i-0abc... --region us-east-1

Notes:
    - A reboot only restarts Docker containers that were running before the
      reboot. A container that was explicitly `docker compose stop`-ped will
      stay down, so after the box is back you may still need to SSH in and run
      `docker compose up -d`.
    - Requires the AWS CLI and credentials with ec2:RebootInstances /
      DescribeInstances (and StopInstances/StartInstances for stop/start).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

from dotenv import load_dotenv

load_dotenv()


def fail(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"❌ {message}")
    sys.exit(1)


def resolve_instance_id(cli_value: str | None) -> str:
    instance_id = cli_value or os.environ.get("EC2_INSTANCE_ID")
    if not instance_id:
        fail(
            "No instance id. Set EC2_INSTANCE_ID in app/.env or pass --instance-id i-0abc..."
        )
    return instance_id


def base_aws_command(region: str | None, profile: str | None) -> list[str]:
    if not shutil.which("aws"):
        fail("AWS CLI not found on PATH. Install it: https://aws.amazon.com/cli/")
    cmd = ["aws", "ec2"]
    if region:
        cmd += ["--region", region]
    if profile:
        cmd += ["--profile", profile]
    return cmd


def run_aws(cmd: list[str]) -> dict:
    """Run an aws CLI command and return parsed JSON (or {} for empty output)."""
    try:
        result = subprocess.run(
            cmd + ["--output", "json"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        fail(f"aws command failed:\n  {' '.join(cmd)}\n  {stderr}")
    out = result.stdout.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {}


def describe(base: list[str], instance_id: str) -> None:
    data = run_aws(base + ["describe-instances", "--instance-ids", instance_id])
    reservations = data.get("Reservations", [])
    if not reservations or not reservations[0].get("Instances"):
        fail(f"Instance {instance_id} not found.")
    inst = reservations[0]["Instances"][0]
    state = inst.get("State", {}).get("Name", "unknown")
    public_ip = inst.get("PublicIpAddress", "(none)")
    itype = inst.get("InstanceType", "(unknown)")

    print(f"🖥  Instance:   {instance_id} ({itype})")
    print(f"   State:      {state}")
    print(f"   Public IP:  {public_ip}")

    status = run_aws(
        base + ["describe-instance-status", "--instance-ids", instance_id]
    )
    statuses = status.get("InstanceStatuses", [])
    if statuses:
        s = statuses[0]
        print(f"   System check:   {s.get('SystemStatus', {}).get('Status', 'n/a')}")
        print(
            f"   Instance check: {s.get('InstanceStatus', {}).get('Status', 'n/a')}"
        )
    else:
        print("   Status checks:  (not reporting - instance may be stopped)")


def wait_for_state(base: list[str], instance_id: str, target: str, timeout: int = 300) -> None:
    print(f"⏳ Waiting for instance to be '{target}' (up to {timeout}s)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = run_aws(base + ["describe-instances", "--instance-ids", instance_id])
        try:
            state = data["Reservations"][0]["Instances"][0]["State"]["Name"]
        except (KeyError, IndexError):
            state = "unknown"
        if state == target:
            print(f"✅ Instance is '{target}'.")
            return
        time.sleep(10)
    print(f"⚠️  Timed out waiting for '{target}'. Check the AWS console.")


def probe_health() -> None:
    base_url = os.environ.get("WEBHOOK_BASE_URL")
    if not base_url:
        return
    url = f"{base_url.rstrip('/')}/health"
    print(f"\n🩺 Probing {url} ...")
    # Give services a moment to come back up after the instance is running.
    for attempt in range(1, 7):
        try:
            req = urllib.request.Request(
                url, headers={"ngrok-skip-browser-warning": "true"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")[:200]
                print(f"   HTTP {resp.status}: {body}")
                if resp.status == 200:
                    print("✅ App is responding.")
                    return
        except Exception as e:  # noqa: BLE001 - best-effort probe
            print(f"   attempt {attempt}/6: not ready yet ({e})")
        time.sleep(10)
    print(
        "⚠️  App did not respond healthy. If the app container was explicitly "
        "stopped, SSH in and run: docker compose up -d"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Reboot/stop/start the project's EC2 instance.")
    parser.add_argument(
        "action",
        nargs="?",
        default="reboot",
        choices=["reboot", "stop", "start", "status"],
        help="Action to perform (default: reboot).",
    )
    parser.add_argument("--instance-id", help="Override EC2_INSTANCE_ID.")
    parser.add_argument("--region", help="Override AWS_REGION.")
    parser.add_argument("--profile", help="Override AWS_PROFILE.")
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait until the instance reaches the expected state, then probe /health.",
    )
    args = parser.parse_args()

    region = args.region or os.environ.get("AWS_REGION")
    profile = args.profile or os.environ.get("AWS_PROFILE")
    base = base_aws_command(region, profile)
    instance_id = resolve_instance_id(args.instance_id)

    if args.action == "status":
        describe(base, instance_id)
        return

    if args.action == "reboot":
        print(f"🔄 Rebooting {instance_id} ...")
        run_aws(base + ["reboot-instances", "--instance-ids", instance_id])
        print("✅ Reboot requested (graceful; can take 1-2 minutes).")
        if args.wait:
            # A reboot keeps the instance in 'running'; give it time, then probe.
            time.sleep(20)
            wait_for_state(base, instance_id, "running")
            probe_health()
        return

    if args.action == "stop":
        print(f"🛑 Stopping {instance_id} ...")
        run_aws(base + ["stop-instances", "--instance-ids", instance_id])
        print("✅ Stop requested.")
        if args.wait:
            wait_for_state(base, instance_id, "stopped")
        return

    if args.action == "start":
        print(f"▶️  Starting {instance_id} ...")
        run_aws(base + ["start-instances", "--instance-ids", instance_id])
        print("✅ Start requested.")
        if args.wait:
            wait_for_state(base, instance_id, "running")
            probe_health()
        return


if __name__ == "__main__":
    main()
