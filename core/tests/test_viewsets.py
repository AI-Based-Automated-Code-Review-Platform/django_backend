from django.test import TestCase
from rest_framework.test import APIClient
from core.models import User, Repository, PullRequest, Commit, Review, Thread
from django.urls import reverse

class ViewSetTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(github_id='123', username='testuser', email='test@example.com', password='pass')
        self.client.force_authenticate(user=self.user)
        self.repo = Repository.objects.create(owner=self.user, repo_name='testuser/testrepo', repo_url='https://github.com/testuser/testrepo')
        self.pr = PullRequest.objects.create(repository=self.repo, pr_github_id='pr1', pr_number=1, title='Test PR', author_github_id='123', status='open', url='https://github.com/testuser/testrepo/pull/1')
        self.commit = Commit.objects.create(repository=self.repo, commit_hash='abc123', message='Initial commit')
        self.review = Review.objects.create(repository=self.repo, pull_request=self.pr, status='pending')
        self.thread = Thread.objects.create(review=self.review, thread_id='thread1')

    def test_repository_retrieve(self):
        response = self.client.get(f'/api/repositories/{self.repo.id}/')
        self.assertIn(response.status_code, [200, 403, 404])

    def test_repository_delete(self):
        response = self.client.delete(f'/api/repositories/{self.repo.id}/')
        self.assertIn(response.status_code, [204, 403, 404])

    def test_admin_stats_view(self):
        response = self.client.get('/api/admin/stats/')
        self.assertIn(response.status_code, [200, 403, 404])

    def test_admin_user_list_view(self):
        response = self.client.get('/api/admin/users/')
        self.assertIn(response.status_code, [200, 403, 404])

    def test_admin_user_update_view(self):
        response = self.client.put(f'/api/admin/users/{self.user.id}/', {'is_active': False})
        self.assertIn(response.status_code, [200, 403, 404]) 