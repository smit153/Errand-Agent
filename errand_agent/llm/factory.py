"""Build the configured real chat model (ModelMux, Claude or Codex). Offline evals never call this."""

from __future__ import annotations

from errand_agent.config import Settings
from errand_agent.errors import ErrandError
from errand_agent.llm.modelmux import DEFAULT_URLS, ModelMuxChatModel, list_models


class ModelConfigError(ErrandError):
    user_message = "The language model isn't configured, so I can't understand messages right now."


def build_chat_model(settings: Settings) -> ModelMuxChatModel:
    if settings.modelmux_api_key is None:
        raise ModelConfigError("ERRAND_MODELMUX_API_KEY is not set")
    provider = settings.modelmux_provider
    url = settings.modelmux_url or DEFAULT_URLS[provider]
    model = settings.modelmux_model
    if not model:
        if provider == "claude":
            model = "sonnet"
        else:
            available = list_models(url, settings.modelmux_api_key)
            if not available:
                raise ModelConfigError("ModelMux codex lists no models; is `modelmux login codex` done?")
            model = available[0]
    return ModelMuxChatModel(url, model, settings.modelmux_api_key, provider=provider)
