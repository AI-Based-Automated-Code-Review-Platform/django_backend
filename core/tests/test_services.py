from django.test import TestCase, RequestFactory
from unittest.mock import patch, MagicMock
from core import services
from django.conf import settings

class ServiceTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.request = self.factory.get('/')
        self.request.session = {}

    def test_generate_and_validate_oauth_state(self):
        state = services.generate_oauth_state(self.request)
        self.assertIn('oauth_state', self.request.session)
        self.assertTrue(services.validate_oauth_state(self.request, state))

    def test_get_github_oauth_redirect_url(self):
        state = 'abc123'
        url = services.get_github_oauth_redirect_url(state)
        self.assertIn('client_id', url)
        self.assertIn('state=abc123', url)

    @patch('core.services.requests.post')
    def test_exchange_code_for_github_token(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {'access_token': 'token123'})
        with patch.object(settings, 'GITHUB_CLIENT_ID', 'id'), patch.object(settings, 'GITHUB_CLIENT_SECRET', 'secret'):
            token = services.exchange_code_for_github_token('code')
            self.assertEqual(token, 'token123')

    @patch('core.services.requests.get')
    def test_get_github_user_info(self, mock_get):
        mock_get.side_effect = [
            MagicMock(status_code=200, json=lambda: {'id': 1, 'login': 'testuser'}),
            MagicMock(status_code=200, json=lambda: [{'email': 'test@example.com', 'primary': True, 'verified': True}])
        ]
        user_data = services.get_github_user_info('token')
        self.assertEqual(user_data['login'], 'testuser')
        self.assertEqual(user_data['email'], 'test@example.com') 