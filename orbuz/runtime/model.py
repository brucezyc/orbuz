"""Explicit OpenAI-compatible transport. Never silently falls back to mocks."""
import os
import httpx


class ChatModel:
    def __init__(self, model, base_url, key_env='DEEPSEEK_API_KEY', timeout=30):
        key = os.environ.get(key_env)
        if not key:
            raise ValueError('Missing credential environment variable: ' + key_env)
        if not base_url.startswith('https://'):
            raise ValueError('HTTPS model endpoint required')
        self.model = model
        self.client = httpx.Client(base_url=base_url.rstrip('/') + '/', timeout=timeout,
                                   headers={'Authorization': 'Bearer ' + key},
                                   follow_redirects=False)

    def complete(self, messages, tools, max_tokens):
        response = self.client.post('chat/completions', json={
            'model': self.model, 'messages': messages, 'tools': tools,
            'max_tokens': max_tokens})
        if response.status_code != 200:
            raise RuntimeError('Model HTTP status ' + str(response.status_code))
        data = response.json()
        choice = data['choices'][0]
        if choice.get('finish_reason') == 'length':
            raise RuntimeError('Model output truncated; no partial actions executed')
        return {'message': choice['message'], 'usage': data.get('usage', {}),
                'model': data.get('model', self.model)}

    def close(self):
        self.client.close()
