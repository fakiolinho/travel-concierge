"""One time setup for the Gateway Lambda: put the Duffel test token in Secrets Manager and tag every seeded order
with its owner (Duffel order metadata.actor_id), which is how the Lambda checks ownership.

Usage: uv run python scripts/prepare_gateway.py   (idempotent; reads DUFFEL_TEST_TOKEN from .env)
"""

import json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
import common  # noqa: E402  (loads .env)
from tools import duffel  # noqa: E402

import boto3  # noqa: E402

SECRET_ID = "travel-concierge/duffel-test-token"

# ponytail: the secret value is set by this script, not CDK, so the token never lands in a CloudFormation template.
# Upgrade: rotate it from a pipeline with access to the Duffel dashboard.
token = os.environ["DUFFEL_TEST_TOKEN"]
assert token.startswith("duffel_test_"), "only Duffel test tokens"
sm = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-east-1"))
try:
    sm.put_secret_value(SecretId=SECRET_ID, SecretString=token)
    print("updated secret", SECRET_ID)
except sm.exceptions.ResourceNotFoundException:
    sm.create_secret(
        Name=SECRET_ID, SecretString=token, Description="Duffel test token, read only by the Gateway Lambda"
    )
    print("created secret", SECRET_ID)

for actor, trips in json.loads(common.HISTORY.read_text()).items():
    for t in trips:
        o = duffel.call("PATCH", f"/orders/{t['order_id']}", {"metadata": {"actor_id": actor}})
        assert o["metadata"]["actor_id"] == actor
    print(f"tagged {len(trips)} orders for {actor}")
