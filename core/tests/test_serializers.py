from django.test import TestCase
from core.models import User, Repository, PullRequest, Commit, Review, Thread, Comment, LLMUsage, ReviewFeedback, WebhookEventLog
from core.serializers import UserSerializer, RepositorySerializer, PRSerializer, CommitSerializer, ReviewSerializer, ThreadSerializer, CommentSerializer, LLMUsageSerializer, ReviewFeedbackSerializer, WebhookEventLogSerializer

class SerializerTests(TestCase):
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

    def test_user_serializer(self):
        data = UserSerializer(self.user).data
        self.assertEqual(data['username'], 'testuser')

    def test_repository_serializer(self):
        data = RepositorySerializer(self.repo).data
        self.assertEqual(data['repo_name'], 'testuser/testrepo')

    def test_pr_serializer(self):
        data = PRSerializer(self.pr).data
        self.assertEqual(data['title'], 'Test PR')

    def test_commit_serializer(self):
        data = CommitSerializer(self.commit).data
        self.assertEqual(data['commit_hash'], 'abc123')

    def test_review_serializer(self):
        data = ReviewSerializer(self.review).data
        self.assertEqual(data['status'], 'pending')

    def test_thread_serializer(self):
        data = ThreadSerializer(self.thread).data
        self.assertEqual(data['thread_id'], 'thread1')

    def test_comment_serializer(self):
        data = CommentSerializer(self.comment).data
        self.assertEqual(data['comment'], 'Test comment')

    def test_llm_usage_serializer(self):
        data = LLMUsageSerializer(self.llm_usage).data
        self.assertEqual(data['llm_model'], 'gpt-4')

    def test_feedback_serializer(self):
        data = ReviewFeedbackSerializer(self.feedback).data
        self.assertEqual(data['rating'], 5)

    def test_webhook_event_serializer(self):
        data = WebhookEventLogSerializer(self.webhook_event).data
        self.assertEqual(data['event_id'], 'evt1') 