"""Bridge service: runs alongside the app on the same laptop. Connects to
each Bambu H2D printer via local MQTT, normalizes telemetry, posts to the
app over the internal Docker network.
"""

__version__ = "0.1.0"
