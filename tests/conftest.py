"""Test-wide setup.

The real app builds its middleware from the settings when it is first imported. A developer's own
`.env` may have login switched on (AUTH_ENABLED=true), which would put the whole app behind a session
and turn tests that call it unauthenticated into 401s. Real environment variables beat the `.env` file,
so pin it off here, before any test module imports the app; the auth tests switch it on themselves.
"""
import os

os.environ["AUTH_ENABLED"] = "false"
