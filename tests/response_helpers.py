"""Fake only the added response model; retain the real message/artifact pipeline."""
import json
from unittest.mock import patch
from langchain_core.messages import AIMessage
from orchestrator.runtime.responses import response_settings


class EchoResponseModel:
    def invoke(self, messages, config=None):
        return AIMessage(content=json.loads(messages[-1].content)['draft'])

    async def ainvoke(self, messages, config=None):
        return self.invoke(messages, config)


def install_response_model(test):
    mocked = patch('orchestrator.runtime.responses.build_response_model',
        side_effect=lambda options: (EchoResponseModel(), response_settings()))
    mocked.start()
    test.addCleanup(mocked.stop)
