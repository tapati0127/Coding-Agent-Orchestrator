import json
import unittest

from orchestrator.runtime.cli_events import EventRenderer, report_text
from orchestrator.runtime.cli_logs import StreamRedactor


class CliEventTests(unittest.TestCase):
    def test_qwen_partial_tool_events_and_final_report(self):
        events = [
            {'type': 'user', 'message': {'content': 'INTERNAL PROMPT'}},
            {'type': 'stream_event', 'event': {'type': 'message_start'}},
            {'type': 'stream_event', 'event': {'type': 'content_block_delta',
                'delta': {'type': 'text_delta', 'text': 'Đang kiểm tra'}}},
            {'type': 'assistant', 'message': {'content': [
                {'type': 'text', 'text': 'Đang kiểm tra'},
                {'type': 'tool_use', 'name': 'run_shell_command', 'input': {'command': 'pytest'}}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'content': '10 passed'}]}},
            {'type': 'stream_event', 'event': {'type': 'message_start'}},
            {'type': 'stream_event', 'event': {'type': 'content_block_delta',
                'delta': {'type': 'text_delta', 'text': 'Đã sửa xong'}}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Đã sửa xong'}]}},
            {'type': 'result', 'is_error': False, 'result': 'Đã sửa xong'},
        ]
        stdout = '\n'.join(json.dumps(event, ensure_ascii=False) for event in events)
        renderer = EventRenderer('qwen')
        # Frame boundaries and Unicode characters need not align with reads.
        rendered = ''.join(renderer.feed(char) for char in stdout) + renderer.feed('', final=True)
        self.assertNotIn('INTERNAL PROMPT', rendered)
        self.assertEqual(rendered.count('Đang kiểm tra'), 1)
        self.assertEqual(rendered.count('Đã sửa xong'), 1)
        self.assertIn('run_shell_command', rendered)
        self.assertIn('pytest', rendered)
        self.assertIn('10 passed', rendered)
        self.assertEqual(report_text(stdout, 'qwen'), 'Đã sửa xong')

    def test_opencode_progress_does_not_enter_assistant_report(self):
        events = [
            {'type': 'step_start', 'part': {}},
            {'type': 'tool_use', 'part': {'tool': 'bash', 'state': {
                'title': 'Run tests', 'output': '10 passed', 'status': 'completed'}}},
            {'type': 'text', 'part': {'text': 'Implemented and tested'}},
            {'type': 'step_finish', 'part': {}},
        ]
        stdout = '\n'.join(json.dumps(event) for event in events) + '\n'
        rendered = EventRenderer('opencode').feed(stdout)
        self.assertIn('Run tests', rendered)
        self.assertIn('10 passed', rendered)
        self.assertIn('Implemented and tested', rendered)
        self.assertEqual(report_text(stdout, 'opencode'), 'Implemented and tested')
        for cli, event in [('qwen', {'type': 'result', 'is_error': True, 'error': {'message': 'Failed'}}),
                           ('opencode', {'type': 'error', 'error': {'message': 'Failed'}})]:
            with self.subTest(cli=cli), self.assertRaisesRegex(RuntimeError, 'Failed'):
                report_text(json.dumps(event), cli)

    def test_secrets_split_across_json_events_and_escaped_tool_inputs_are_redacted(self):
        secret = 'private"token'
        events = [
            {'type': 'stream_event', 'event': {'type': 'content_block_delta',
                'delta': {'type': 'text_delta', 'text': 'private"'}}},
            {'type': 'stream_event', 'event': {'type': 'content_block_delta',
                'delta': {'type': 'text_delta', 'text': 'token\n'}}},
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'test', 'input': {'value': secret}}]}},
        ]
        redactor, renderer = StreamRedactor([secret]), EventRenderer('qwen')
        output = ''
        for event in events:
            output += redactor.feed(renderer.feed(json.dumps(event) + '\n'))
        output += redactor.feed(renderer.feed('', final=True), final=True)
        self.assertNotIn(secret, output)
        self.assertNotIn('private\\"token', output)
        self.assertEqual(output.count('[REDACTED]'), 2)
