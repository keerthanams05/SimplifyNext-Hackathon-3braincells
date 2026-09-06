"""
Thread-safe AWS handles, shared by every agent.

Why this exists: boto3 *clients* are documented as thread-safe, but boto3
*resources* (what we use for DynamoDB) and Sessions are NOT — the docs are
explicit that a resource should not be shared across threads. Every agent
used to create its own module-level `boto3.resource(...)` at import time,
which is fine while everything runs serially but quietly unsafe the moment
the pipeline runs agents concurrently.

Rather than make each agent thread-aware, these two names are proxies: each
thread that touches one gets its own Session-backed object, created on
first use. Call sites don't change at all — `dynamodb.Table(...)` and
`bedrock.converse(...)` work exactly as before.

    from aws_clients import dynamodb, bedrock
"""

import threading

import boto3

from config import AWS_REGION, BEDROCK_REGION


class _ThreadLocalProxy:
    """Forwards attribute access to a per-thread instance built by `factory`.

    Created lazily, so importing an agent never opens a connection and the
    CLI entry points stay as fast as they were.
    """

    def __init__(self, factory, label):
        self._factory = factory
        self._label = label
        self._local = threading.local()

    def _instance(self):
        instance = getattr(self._local, "instance", None)
        if instance is None:
            instance = self._factory()
            self._local.instance = instance
        return instance

    def __getattr__(self, name):
        # Only reached for names this proxy doesn't define itself, so every
        # real API call (Table, converse, get_item, ...) lands here.
        return getattr(self._instance(), name)

    def __repr__(self):
        return f"<thread-local {self._label} for {threading.current_thread().name}>"


# A fresh Session per thread: sharing one Session across threads is the
# other half of the same warning in the boto3 docs.
dynamodb = _ThreadLocalProxy(
    lambda: boto3.Session().resource("dynamodb", region_name=AWS_REGION),
    "dynamodb resource",
)

bedrock = _ThreadLocalProxy(
    lambda: boto3.Session().client("bedrock-runtime", region_name=BEDROCK_REGION),
    "bedrock-runtime client",
)
