import asyncio
from django.test import TestCase
from unittest.mock import AsyncMock, patch
from core.webhooks.handlers import GitHubWebhookHandler

class AsyncHandlerTests(TestCase):
    def setUp(self):
        self.handler = GitHubWebhookHandler()

    @patch.object(GitHubWebhookHandler, 'handle_pull_request', new_callable=AsyncMock)
    @patch.object(GitHubWebhookHandler, 'handle_push', new_callable=AsyncMock)
    @patch.object(GitHubWebhookHandler, 'handle_member', new_callable=AsyncMock)
    def test_handle_event_dispatch(self, mock_member, mock_push, mock_pr):
        asyncio.get_event_loop().run_until_complete(self.handler.handle_event('pull_request', {}))
        mock_pr.assert_awaited()
        asyncio.get_event_loop().run_until_complete(self.handler.handle_event('push', {}))
        mock_push.assert_awaited()
        asyncio.get_event_loop().run_until_complete(self.handler.handle_event('member', {}))
        mock_member.assert_awaited() 