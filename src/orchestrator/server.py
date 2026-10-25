"""Attach the outer graph.py process lifecycle to the LangGraph Agent Server."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from langgraph.runtime import get_runtime

from orchestrator.config import Settings


@asynccontextmanager
async def lifespan(app):
    from graph import graph_lifespan

    async with graph_lifespan(get_runtime(), Settings.from_env()):
        yield


app = FastAPI(title="Qwen Orchestrator", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=lifespan)
