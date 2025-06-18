from django.test import TestCase
from unittest.mock import patch, AsyncMock
from core.webhooks.handlers import GitHubWebhookHandler

class WebhookTests(TestCase):
    @patch.object(GitHubWebhookHandler, 'handle_pull_request', new_callable=AsyncMock)
    @patch.object(GitHubWebhookHandler, 'handle_push', new_callable=AsyncMock)
    @patch.object(GitHubWebhookHandler, 'handle_member', new_callable=AsyncMock)
    def test_handle_event_dispatch(self, mock_member, mock_push, mock_pr):
        handler = GitHubWebhookHandler()
        # Test pull_request event
        self.run_async(handler.handle_event('pull_request', {}))
        mock_pr.assert_awaited()
        # Test push event
        self.run_async(handler.handle_event('push', {}))
        mock_push.assert_awaited()
        # Test member event
        self.run_async(handler.handle_event('member', {}))
        mock_member.assert_awaited()

    def run_async(self, coro):
        import asyncio
        return asyncio.get_event_loop().run_until_complete(coro) 