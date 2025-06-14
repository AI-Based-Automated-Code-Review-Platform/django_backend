from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin # Import necessary classes

# Create your models here.
class TimestampMixin(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

class UserManager(BaseUserManager):
    def create_user(self, github_id, username, email=None, password=None, **extra_fields):
        if not github_id:
            raise ValueError('Users must have a GitHub ID')
        if not username:
            raise ValueError('Users must have a username')
        
        email = self.normalize_email(email) if email else None
        user = self.model(github_id=github_id, username=username, email=email, **extra_fields)
        user.set_password(password) # Handles hashing
        user.save(using=self._db)
        return user

    def create_superuser(self, github_id, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_admin', True) # Ensure is_admin is also set for superuser

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        
        return self.create_user(github_id, username, email, password, **extra_fields)

class User(AbstractBaseUser, PermissionsMixin, TimestampMixin):
    github_id = models.CharField(max_length=255, unique=True, db_index=True)
    username = models.CharField(max_length=255, unique=True) # Assuming username from GitHub is unique
    email = models.EmailField(max_length=255, unique=True, null=True, blank=True) # Email can be null from GitHub
    is_admin = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False) # Required by Django admin
    is_active = models.BooleanField(default=True) # Required by Django auth
    github_access_token = models.TextField(null=True, blank=True)
    avatar_url = models.URLField(max_length=500, null=True, blank=True)
    is_ai_user = models.BooleanField(default=False) # New field

    objects = UserManager()

    USERNAME_FIELD = 'username' # Or 'github_id' if preferred for login
    REQUIRED_FIELDS = ['github_id'] # Fields prompted for when creating superuser, besides USERNAME_FIELD and password

    def __str__(self):
        return self.username

class Repository(TimestampMixin):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='repositories', on_delete=models.CASCADE)
    github_native_id = models.IntegerField(unique=True, null=True, blank=True, db_index=True)
    repo_name = models.CharField(max_length=255) 
    repo_url = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    coding_standards = models.JSONField(null=True, blank=True)
    code_metrics = models.JSONField(null=True, blank=True)
    llm_preference = models.CharField(max_length=255, null=True, blank=True)
    webhook_url = models.CharField(max_length=255, null=True, blank=True)
    webhook_secret = models.CharField(max_length=255, null=True, blank=True)
    webhook_last_event_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('owner', 'repo_name')
        verbose_name_plural = "Repositories"

    def __str__(self):
        return self.repo_name

class RepoCollaborator(TimestampMixin):
    ROLE_CHOICES = [
        ('owner', 'Owner'),
        ('contributor', 'Contributor'),
        ('member', 'Member'), # Added based on webhook handling
        ('admin', 'Admin'),   # Added based on webhook handling (GitHub permission)
        ('pull', 'Pull'),     # GitHub permission
        ('push', 'Push'),     # GitHub permission
    ]
    repository = models.ForeignKey(Repository, related_name='collaborators', on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='repo_collaborations', on_delete=models.CASCADE)
    role = models.CharField(max_length=50, choices=ROLE_CHOICES)

    class Meta:
        unique_together = ('repository', 'user')

    def __str__(self):
        return f"{self.user.username} - {self.repository.repo_name} ({self.role})"

class PullRequest(TimestampMixin):
    PR_STATUS_CHOICES = [
        ('open', 'Open'),
        ('closed', 'Closed'),
        ('merged', 'Merged'),
    ]
    repository = models.ForeignKey(Repository, related_name='pull_requests', on_delete=models.CASCADE)
    pr_github_id = models.CharField(max_length=255,unique=True) # GitHub's own ID for the PR, not our DB ID.
    pr_number = models.IntegerField() 
    title = models.CharField(max_length=255)
    author_github_id = models.CharField(max_length=255)
    status = models.CharField(max_length=50, choices=PR_STATUS_CHOICES)
    url = models.CharField(max_length=255)
    body = models.TextField(null=True, blank=True)
    head_sha = models.CharField(max_length=255, null=True, blank=True)
    base_sha = models.CharField(max_length=255, null=True, blank=True)

    def __str__(self):
        return f"PR #{self.pr_number}: {self.title}"

class Commit(TimestampMixin):
    repository = models.ForeignKey(Repository, related_name='commits', on_delete=models.CASCADE)
    commit_hash = models.CharField(max_length=255)
    author_github_id = models.CharField(max_length=255, null=True, blank=True)
    committer_github_id = models.CharField(max_length=255, null=True, blank=True) # Added from webhook logic
    message = models.TextField()
    url = models.CharField(max_length=255, null=True, blank=True) # Added from webhook logic
    timestamp = models.DateTimeField(null=True, blank=True) # Added from webhook logic

    class Meta:
        unique_together = ('repository', 'commit_hash')

    def __str__(self):
        return self.commit_hash[:12]

class Review(TimestampMixin):
    REVIEW_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('processing', 'Processing'), # Added from webhook logic
        ('pending_analysis', 'Pending Analysis') # Added from webhook logic
    ]
    repository = models.ForeignKey(Repository, related_name='reviews', on_delete=models.CASCADE)
    pull_request = models.ForeignKey(PullRequest, related_name='reviews', on_delete=models.CASCADE, null=True, blank=True)
    commit = models.ForeignKey(Commit, related_name='reviews', on_delete=models.CASCADE, null=True, blank=True)
    parent_review = models.ForeignKey('self', related_name='re_reviews', on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=20, choices=REVIEW_STATUS_CHOICES, default='pending')
    review_data = models.JSONField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True) # New field for storing error messages
    # user = models.ForeignKey(User, related_name='reviews', on_delete=models.CASCADE) # Consider who owns/requested the review

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(pull_request__isnull=False) | models.Q(commit__isnull=False),
                name='check_review_context'
            )
        ]

    def __str__(self):
        if self.pull_request:
            return f"Review for PR #{self.pull_request.pr_number}"
        elif self.commit:
            return f"Review for Commit {self.commit.commit_hash[:7]}"
        return f"Review {self.id}"

class Thread(TimestampMixin):
    review = models.ForeignKey(Review, related_name='threads', on_delete=models.CASCADE)
    thread_id = models.CharField(max_length=255, unique=True, help_text="LangGraph thread ID") # Added unique=True
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='created_threads', on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=20, default='open')
    title = models.CharField(max_length=255, blank=True, null=True, help_text="Optional thread title or topic")
    thread_type = models.CharField(max_length=50, default='main', help_text="Type of thread (main, feedback, followup, etc.)")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True) # To track overall thread activity
    last_comment_at = models.DateTimeField(null=True, blank=True) # New field for signal

    def __str__(self):
        return f"Thread for Review {self.review.id} - {self.thread_id}"

class Comment(TimestampMixin):
    COMMENT_TYPE_CHOICES = [
        ('request', 'Request'),
        ('response', 'Response'),
        ('note', 'Note'),
    ]
    thread = models.ForeignKey(Thread, related_name='comments', on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='comments', on_delete=models.CASCADE)
    comment = models.TextField()
    comment_data = models.JSONField(null=True, blank=True)
    type = models.CharField(max_length=50, choices=COMMENT_TYPE_CHOICES)
    parent_comment = models.ForeignKey('self', related_name='replies', null=True, blank=True, on_delete=models.SET_NULL)

    def __str__(self):
        return f"Comment by {self.user.username} on Thread {self.thread.id}"

class LLMUsage(TimestampMixin):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='llm_usages', on_delete=models.CASCADE)
    review = models.ForeignKey(Review, related_name='llm_usages', null=True, blank=True, on_delete=models.CASCADE)
    llm_model = models.CharField(max_length=255)
    input_tokens = models.IntegerField()
    output_tokens = models.IntegerField()
    cost = models.FloatField()

    def __str__(self):
        return f"LLM Usage by {self.user.username} for Review {self.review.id if self.review else 'N/A'}"

class ReviewFeedback(TimestampMixin):
    review = models.ForeignKey(Review, related_name='feedbacks', on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='review_feedbacks', on_delete=models.CASCADE)
    rating = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    feedback = models.TextField()

    def __str__(self):
        return f"Feedback by {self.user.username} for Review {self.review.id} (Rating: {self.rating})"

class WebhookEventLog(TimestampMixin):
    repository = models.ForeignKey(Repository, related_name='webhook_events', on_delete=models.CASCADE, null=True, blank=True)
    event_id = models.CharField(max_length=255, unique=True, null=True, blank=True, help_text="GitHub event ID (X-GitHub-Delivery)")
    event_type = models.CharField(max_length=100, help_text="e.g., pull_request, push")
    payload = models.JSONField()
    headers = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=20, default='received', choices=[
        ('received', 'Received'),
        ('processed', 'Processed'),
        ('failed', 'Failed'),
    ])
    error_message = models.TextField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        repo_name = self.repository.repo_name if self.repository else "Unknown"
        return f"Event {self.event_id} ({self.event_type}) - {repo_name} - {self.status}"
