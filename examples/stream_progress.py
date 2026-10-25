"""python -m examples.stream_progress 'Yêu cầu của bạn'."""
import argparse
import asyncio
from pathlib import Path

from dotenv import load_dotenv
from graph import build_graph


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('request')
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    displayed = {}
    async for namespace, update in build_graph().astream(
        {'messages': [('user', args.request)]}, stream_mode='updates', subgraphs=True,
    ):
        for fields in update.values():
            if not isinstance(fields, dict):
                continue
            for message in fields.get('messages', []):
                if displayed.get(message.id) != message.content:
                    # A UI should upsert the existing bubble by message.id.
                    displayed[message.id] = message.content
                    print(f'[{message.name or message.type}] {message.content}', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
