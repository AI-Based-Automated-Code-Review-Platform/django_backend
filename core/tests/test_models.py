from django.test import TestCase
from django.core.exceptions import ValidationError
from core.models import User, Repository, PullRequest, Commit, Review, Thread, Comment, LLMUsage, ReviewFeedback, WebhookEventLog

class ModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(github_id='123', username='testuser', email='test@example.com', password='pass')
        self.repo = Repository.objects.create(owner=self.user, repo_name='testuser/testrepo', repo_url='https://github.com/testuser/testrepo')
        self.pr = PullRequest.objects.create(repository=self.repo, pr_github_id='pr1', pr_number=1, title='Test PR', author_github_id='123', status='open', url='https://github.com/testuser/testrepo/pull/1')
        self.commit = Commit.objects.create(repository=self.repo, commit_hash='abc123', message='Initial commit')
        self.review = Review.objects.create(repository=self.repo, pull_request=self.pr, status='pending')
        self.thread = Thread.objects.create(review=self.review, thread_id='thread1')
        self.comment = Comment.objects.create(thread=self.thread, user=self.user, comment='Test comment', type='request')
        self.llm_usage = LLMUsage.objects.create(user=self.user, review=self.review, llm_model='gpt-4', input_tokens=10, output_tokens=20, cost=0.01)
        self.feedback = ReviewFeedback.objects.create(review=self.review, user=self.user, rating=5, feedback='Great review!')
        self.webhook_event = WebhookEventLog.objects.create(repository=self.repo, event_id='evt1', event_type='push', payload={})

    def test_user_str(self):
        self.assertEqual(str(self.user), 'testuser')

    def test_repo_str(self):
        self.assertEqual(str(self.repo), 'testuser/testrepo')

    def test_pr_str(self):
        self.assertIn('Test PR', str(self.pr))

    def test_commit_str(self):
        self.assertEqual(str(self.commit), 'abc123')

    def test_review_str(self):
        self.assertIn('Review for PR', str(self.review))

    def test_thread_str(self):
        self.assertIn('Thread for Review', str(self.thread))

    def test_comment_str(self):
        self.assertIn('Comment by', str(self.comment))

    def test_llm_usage_str(self):
        self.assertIn('LLM Usage by', str(self.llm_usage))

    def test_feedback_str(self):
        self.assertIn('Feedback by', str(self.feedback))

    def test_webhook_event_str(self):
        self.assertIn('Event evt1', str(self.webhook_event))

    def test_review_constraint(self):
        # Should fail if both pull_request and commit are None
        review = Review(repository=self.repo, status='pending')
        with self.assertRaises(ValidationError):
            review.full_clean() 