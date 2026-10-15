"""Test isolation entry: purge credentials, disable dotenv, block external sockets."""
from tests import isolation  # noqa: F401  — side effects: env purge + socket block
