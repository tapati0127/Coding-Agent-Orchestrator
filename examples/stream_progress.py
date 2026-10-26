"""python -m examples.stream_progress 'Yêu cầu của bạn'."""
import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from graph import build_graph


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('request')
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    displayed = {}
    workflow = build_graph(checkpointer=InMemorySaver())
    config = {'configurable': {'thread_id': uuid4().hex}}
    pending = {'messages': [('user', args.request)]}
    while True:
        interrupts = []
        async for namespace, mode, update in workflow.astream(
            pending, config, stream_mode=['updates', 'custom'], subgraphs=True,
        ):
            if mode == 'custom':
                fields_list = [update.get('update', {})]
            else:
                interrupts.extend(update.get('__interrupt__', ()))
                fields_list = update.values()
            for fields in fields_list:
                if not isinstance(fields, dict):
                    continue
                for message in fields.get('messages', []):
                    if displayed.get(message.id) != message.content:
                        # A UI should upsert the existing bubble by message.id.
                        displayed[message.id] = message.content
                        print(f'[{message.name or message.type}] {message.content}', flush=True)
        if not interrupts:
            break
        question = interrupts[-1].value
        prompt = question.get('question', str(question)) if isinstance(question, dict) else str(question)
        response = ''
        while not response.strip():
            response = await asyncio.to_thread(input, prompt + '\n> ')
        pending = Command(resume=response)


if __name__ == '__main__':
    asyncio.run(main())
