"""Custom resource that owns the demo's API key.

CloudFormation cannot create an SSM ``SecureString`` directly, and generating the
key at synth time would write it into the template and into ``cdk.out`` on disk.
So the key is generated here, inside the deploy, and only ever stored encrypted.

Lifecycle:

* **Create** generates a key and writes it to Parameter Store.
* **Update** returns the key already stored. Deploying twice must not rotate the
  secret: the UI and any shell the presenter has open would start getting 401s
  for no visible reason. If the parameter has gone missing, a new key is written.
* **Delete** removes the parameter, so ``cdk destroy`` leaves nothing behind.

The generated value is returned as response data so the stack can surface it as
an output. That makes it readable by anyone who can call ``DescribeStacks`` on the
account, which is the tradeoff this demo accepts in exchange for a one-command
deploy. It is never logged here.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")

#: 32 bytes rendered as hex. Same strength as the `openssl rand -hex 32` the
#: README used to ask for by hand.
KEY_BYTES = 32

DESCRIPTION = "Shared API key for the guardrails demo. Created by CloudFormation."


def _existing_key(name: str) -> str | None:
    """The stored key, or None when the parameter is absent or empty."""
    try:
        response = ssm.get_parameter(Name=name, WithDecryption=True)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise
    return (response["Parameter"]["Value"] or "").strip() or None


def _write_key(name: str) -> str:
    key = secrets.token_hex(KEY_BYTES)
    ssm.put_parameter(
        Name=name,
        Description=DESCRIPTION,
        Value=key,
        Type="SecureString",
        Overwrite=True,
    )
    return key


def _delete_key(name: str) -> None:
    try:
        ssm.delete_parameter(Name=name)
    except ClientError as exc:
        # Already gone is the desired end state, not a failure.
        if exc.response.get("Error", {}).get("Code") != "ParameterNotFound":
            raise


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    request_type = event["RequestType"]
    name = event["ResourceProperties"]["ParameterName"]

    # Stable across updates so CloudFormation never treats an update as a
    # replacement and deletes the parameter straight after writing it.
    physical_id = name

    if request_type == "Delete":
        logger.info("deleting %s", name)
        _delete_key(name)
        return {"PhysicalResourceId": physical_id}

    if request_type == "Update":
        key = _existing_key(name)
        if key is None:
            logger.info("parameter %s was missing on update, writing a new key", name)
            key = _write_key(name)
        else:
            logger.info("reusing the key already in %s", name)
    else:
        logger.info("creating %s", name)
        key = _write_key(name)

    return {
        "PhysicalResourceId": physical_id,
        "Data": {"ApiKey": key, "ParameterName": name},
    }
