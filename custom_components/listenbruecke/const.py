"""Native Home Assistant integration constants."""

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "listenbruecke"
CONF_URL = "url"
CONF_TOKEN = "token"
UPDATE_INTERVAL = timedelta(seconds=30)
PLATFORMS = [Platform.TODO, Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH, Platform.NUMBER, Platform.BUTTON]
