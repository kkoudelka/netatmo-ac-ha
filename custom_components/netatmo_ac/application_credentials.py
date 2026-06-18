"""OAuth2 application credentials for Netatmo Smart AC Controller."""
from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant

from .const import NETATMO_AUTH_URL, NETATMO_TOKEN_URL


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    return AuthorizationServer(
        authorize_url=NETATMO_AUTH_URL,
        token_url=NETATMO_TOKEN_URL,
    )
