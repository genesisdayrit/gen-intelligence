"""Shared configuration — single source of truth for env-based settings.

Import from here instead of calling os.getenv() directly in each file.
See docs/guides/config-migration.md for the list of files to migrate.
"""

import os

import pytz
import redis
from dotenv import load_dotenv

load_dotenv()

# Timezone
SYSTEM_TIMEZONE_STR = os.getenv("SYSTEM_TIMEZONE", "America/Los_Angeles")
SYSTEM_TZ = pytz.timezone(SYSTEM_TIMEZONE_STR)

# Redis
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    password=os.getenv("REDIS_PASSWORD", None),
    decode_responses=True,
)
