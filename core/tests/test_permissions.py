from django.test import TestCase, RequestFactory
from core.models import User, Repository, Thread, PullRequest, Review
from core.permissions import IsRepositoryOwner, CanAccessRepository
from unittest.mock import patch, MagicMock

class PermissionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(github_id='123', username='testuser', email='test@example.com', password='pass')
        self.other_user = User.objects.create_user(github_id='456', username='otheruser', email='other@example.com', password='pass')
        self.repo = Repository.objects.create(owner=self.user, repo_name='testuser/testrepo', repo_url='https://github.com/testuser/testrepo')
        self.pr = PullRequest.objects.create(repository=self.repo, pr_github_id='pr1', pr_number=1, title='Test PR', author_github_id='123', status='open', url='https://github.com/testuser/testrepo/pull/1')
        self.review = Review.objects.create(repository=self.repo, pull_request=self.pr, status='pending')
        self.thread = Thread.objects.create(review=self.review, thread_id='thread1')
        self.factory = RequestFactory()

    def test_is_repository_owner(self):
        perm = IsRepositoryOwner()
        request = self.factory.get('/')
        request.user = self.user
        self.assertTrue(perm.has_object_permission(request, None, self.repo))
        request.user = self.other_user
        self.assertFalse(perm.has_object_permission(request, None, self.repo))

    @patch('core.permissions.get_repo_collaborators_from_github', return_value=[])
    def test_can_access_repository_owner(self, mock_collabs):
        perm = CanAccessRepository()
        request = self.factory.get('/')
        request.user = self.user
        self.assertTrue(perm.has_object_permission(request, None, self.repo))

    @patch('core.permissions.get_repo_collaborators_from_github', return_value=[])
    def test_can_access_repository_not_owner(self, mock_collabs):
        perm = CanAccessRepository()
        request = self.factory.get('/')
        request.user = self.other_user
        self.assertFalse(perm.has_object_permission(request, None, self.repo)) 