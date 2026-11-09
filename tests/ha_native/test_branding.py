"""HA's own local brand resolver must serve our packaged integration artwork."""

from homeassistant.components.brands import BrandsIntegrationView
from homeassistant.loader import async_get_integration

from custom_components.listenbruecke.const import DOMAIN


async def test_local_brand_assets_resolve_without_external_brands_request(hass):
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.has_branding
    view = BrandsIntegrationView(hass)
    for image in ("icon.png", "logo.png", "icon@2x.png", "logo@2x.png"):
        response = await view._serve_from_custom_integration(DOMAIN, image)
        assert response is not None
        assert response.content_type == "image/png"
        assert response.body.startswith(b"\x89PNG\r\n\x1a\n")
